<#
.SYNOPSIS
  One-time setup for Pionir's Apify Store publishing. Run it yourself.

.DESCRIPTION
  Asks for your Apify API token (hidden), checks its shape locally, asks Apify whose account
  it is (GET /v2/users/me - a read, nothing is created or changed), and ONLY if that works
  saves the token to the secrets folder, readable only by you. Prints the Apify username.
  Never prints the token.

  Where the token comes from: https://console.apify.com > Settings > API & Integrations >
  Personal API tokens > Create a new token (name it "Pionir"). Before the first Actor can
  earn, in the Apify Console also: accept the monetization terms (an Actor's Publication >
  Monetization tab) and set a payout method (Settings > Payouts; PayPal, $20 minimum).

  Every Actor publish still waits for your approval on Discord or the phone: this token only
  lets Pionir carry out what you approve (apify.publish), and read your Actors' figures
  (apify.stats). If it is ever rejected, delete the token in the Apify Console, create a new
  one, and run this again.

.PARAMETER TokenFile
  Where the token is kept. Default: $env:USERPROFILE\.pionir\secrets\apify-token.txt

.PARAMETER ApiUrl
  The Apify API base. Default: https://api.apify.com
#>
[CmdletBinding()]
param(
    [string]$TokenFile = "",
    [string]$ApiUrl = "https://api.apify.com"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $TokenFile) { $TokenFile = Join-Path $env:USERPROFILE ".pionir\secrets\apify-token.txt" }
$ApiUrl = $ApiUrl.TrimEnd('/')
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

Write-Host ""
Write-Host "Pionir Apify Store publishing - setup" -ForegroundColor Cyan
Write-Host "  token file : $TokenFile"
Write-Host "  api        : $ApiUrl"
Write-Host ""

function Get-TokenProblem([string]$t) {
    if ($t.Length -lt 20) {
        return "that is too short to be an Apify token - the paste probably did not go in. In this window, paste with a RIGHT-CLICK; Ctrl+V does not always work in a hidden prompt."
    }
    if ($t.Length -gt 120) {
        return "that is too long to be an Apify token. Copy just the token and try again."
    }
    if ($t -match '^https?:') {
        return "that is a web address, not a token. Copy the token itself from the Apify Console."
    }
    if ($t -notmatch '^[A-Za-z0-9_\-]+$') {
        return "that has characters an Apify token does not have (spaces, quotes or line breaks?)."
    }
    return $null
}

if (Test-Path -LiteralPath $TokenFile) {
    $answer = Read-Host "An Apify token is already saved. Replace it? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') {
        Write-Host "  kept the saved token."
        exit 0
    }
}
$secure = Read-Host "Paste the Apify API token (input is hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
$token = $token.Trim()
if (-not $token) {
    Write-NotOk "no token entered; nothing saved"
    exit 1
}
$problem = Get-TokenProblem $token
if ($problem) {
    Write-NotOk "not saved: $problem"
    exit 1
}

# ---- one read-only call: whose account is it? ------------------------------------------
Write-Host ""
Write-Host "Checking with Apify (a read only)..." -ForegroundColor Cyan
$me = $null
$code = 0
$why = ""
try {
    $headers = @{ "Authorization" = "Bearer $token"; "Accept" = "application/json" }
    $me = Invoke-RestMethod -Uri ($ApiUrl + "/v2/users/me") -Method Get -Headers $headers `
        -UserAgent "pionir-apify-setup/0.1" -TimeoutSec 30
}
catch {
    $response = $_.Exception.Response
    if ($null -ne $response) { $code = [int]$response.StatusCode }
    $why = [string]$_.Exception.Message
    $why = $why.Replace($token, "<redacted>")
    if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
}
$headers = $null

$username = ""
if ($null -ne $me -and $me.PSObject.Properties.Name -contains "data") {
    if ($me.data.PSObject.Properties.Name -contains "username") { $username = [string]$me.data.username }
}
if (-not $username) {
    if ($code -eq 401 -or $code -eq 403) {
        Write-NotOk "Apify rejected the token (HTTP $code). Create a new one in the Apify Console and run this again. Nothing saved."
    }
    elseif ($code -ne 0) {
        Write-NotOk "Apify refused the check (HTTP $code): $why. Nothing saved."
    }
    else {
        Write-NotOk "could not reach Apify to check the token: $why. Nothing saved."
    }
    $token = $null
    exit 1
}

# ---- save it, readable only by you ----------------------------------------------------------
$dir = Split-Path -Parent $TokenFile
New-Item -ItemType Directory -Force -Path $dir | Out-Null
[IO.File]::WriteAllText($TokenFile, $token, $utf8NoBom)
$token = $null
Remove-Variable -Name token -ErrorAction SilentlyContinue
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $TokenFile /inheritance:r /grant:r "$($who):(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (could not restrict the token file's permissions; it is saved anyway)" -ForegroundColor Yellow
}

Write-Ok "connected to Apify as $username"
Write-Host "  token saved to $TokenFile (not shown)"
Write-Host ""
Write-Host "OK - Apify publishing is set up. Every Actor publish still waits for your yes." -ForegroundColor Green
exit 0
