<#
.SYNOPSIS
  One-time setup for Pionir's dev.to cross-posting. Run it yourself.

.DESCRIPTION
  Asks for the dev.to API key (hidden), checks its shape locally, asks dev.to who it
  belongs to (GET /api/users/me with the key), and ONLY if that works saves the key to
  the secrets folder. Prints the dev.to username it connected to. Never prints the key.

  Where the key comes from: dev.to > Settings > Extensions > "DEV Community API Keys" >
  give it a description (e.g. "Pionir") > Generate API Key, signed in as the account the
  Dokaz posts should appear under.

  Pionir only cross-posts a blog post that is already live on api.dokaz.net, and every
  cross-post still waits for your yes. If it ever says "the dev.to API key was rejected",
  revoke the old key on dev.to, generate a new one, and run this again.

  After this, check it any time with:  python -m pionir devto-check

.PARAMETER KeyFile
  Where the key is kept. Default: $env:USERPROFILE\.pionir\secrets\devto-api-key.txt

.PARAMETER ApiUrl
  The dev.to API base. Default: https://dev.to/api
#>
[CmdletBinding()]
param(
    [string]$KeyFile = "",
    [string]$ApiUrl = "https://dev.to/api"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $KeyFile) { $KeyFile = Join-Path $env:USERPROFILE ".pionir\secrets\devto-api-key.txt" }
$ApiUrl = $ApiUrl.TrimEnd('/')
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
# Windows PowerShell 5.1 may default to TLS 1.0, which dev.to refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

Write-Host ""
Write-Host "Pionir dev.to cross-posting - setup" -ForegroundColor Cyan
Write-Host "  key file : $KeyFile"
Write-Host "  api      : $ApiUrl"
Write-Host ""

# ---- is it even an API key? (checked locally; the value is never shown) -----------------
function Get-KeyProblem([string]$k) {
    if ($k.Length -lt 16) {
        return "that is too short to be a dev.to API key - the paste probably did not go in. In this window, paste with a RIGHT-CLICK; Ctrl+V does not always work in a hidden prompt."
    }
    if ($k.Length -gt 128) {
        return "that is too long to be a dev.to API key. Copy just the key and try again."
    }
    if ($k -match '^https?:') {
        return "that is a web address, not an API key. Copy the key itself from dev.to > Settings > Extensions."
    }
    if ($k -notmatch '^[A-Za-z0-9_\-]+$') {
        return "that has characters a dev.to API key does not have (spaces, quotes or line breaks?). Copy just the key and try again."
    }
    return $null
}

# ---- the key: read hidden ---------------------------------------------------------------
$key = ""
if (Test-Path -LiteralPath $KeyFile) {
    $answer = Read-Host "A dev.to API key is already saved. Replace it? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') {
        Write-Host "  kept the saved key. Check it with: python -m pionir devto-check"
        exit 0
    }
}
$secure = Read-Host "Paste the dev.to API key (input is hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
$key = $key.Trim()
if (-not $key) {
    Write-NotOk "no key entered; nothing saved"
    exit 1
}
$problem = Get-KeyProblem $key
if ($problem) {
    Write-NotOk "not saved: $problem"
    $key = $null
    exit 1
}

# ---- ask dev.to who it belongs to ---------------------------------------------------------
Write-Host ""
Write-Host "Checking with dev.to..." -ForegroundColor Cyan
$me = $null
$code = 0
$why = ""
try {
    $headers = @{ "api-key" = $key; "Accept" = "application/vnd.forem.api-v1+json" }
    $me = Invoke-RestMethod -Uri ($ApiUrl + "/users/me") -Method Get -Headers $headers `
        -UserAgent "pionir-devto-setup/0.1" -TimeoutSec 30
}
catch {
    $response = $_.Exception.Response
    if ($null -ne $response) { $code = [int]$response.StatusCode }
    $why = [string]$_.Exception.Message
    if ($null -ne $_.ErrorDetails -and $_.ErrorDetails.Message) {
        try {
            $body = $_.ErrorDetails.Message | ConvertFrom-Json
            if ($body.PSObject.Properties.Name -contains "error") { $why = [string]$body.error }
        }
        catch {
            $why = [string]$_.ErrorDetails.Message
        }
    }
    $why = $why.Replace($key, "<redacted>")
    if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
}
$headers = $null

if ($null -eq $me) {
    if ($code -eq 401 -or $code -eq 403) {
        Write-NotOk "dev.to rejected the key (HTTP $code). Generate a new one on dev.to > Settings > Extensions and run this again. Nothing saved."
    }
    elseif ($code -ge 400 -and $code -lt 500) {
        Write-NotOk "dev.to refused the check (HTTP $code): $why. Nothing saved."
    }
    else {
        Write-NotOk "could not reach dev.to to check the key (HTTP $code): $why. Nothing saved."
    }
    $key = $null
    Remove-Variable -Name key -ErrorAction SilentlyContinue
    exit 1
}

$username = ""
if ($me.PSObject.Properties.Name -contains "username") { $username = [string]$me.username }
if (-not $username) {
    Write-NotOk "dev.to answered without a username; nothing saved."
    $key = $null
    Remove-Variable -Name key -ErrorAction SilentlyContinue
    exit 1
}

# ---- save it, readable only by you -----------------------------------------------------------
$keyDir = Split-Path -Parent $KeyFile
New-Item -ItemType Directory -Force -Path $keyDir | Out-Null
[IO.File]::WriteAllText($KeyFile, $key, $utf8NoBom)
$key = $null
Remove-Variable -Name key -ErrorAction SilentlyContinue
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $KeyFile /inheritance:r /grant:r "$($who):(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (could not restrict the key file's permissions; it is saved anyway)" -ForegroundColor Yellow
}

Write-Ok "connected to dev.to as @$username"
Write-Host "  key saved to $KeyFile (not shown)"
Write-Host ""
Write-Host "OK - dev.to cross-posting is set up. Every cross-post still waits for your yes." -ForegroundColor Green
Write-Host "Check it any time with: python -m pionir devto-check"
exit 0
