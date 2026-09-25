<#
.SYNOPSIS
  One-time setup for Pionir's Gumroad products. Run it yourself.

.DESCRIPTION
  Asks for the Gumroad access token (hidden), checks its shape locally, asks Gumroad whose
  account it is (GET /v2/user with the token as a Bearer header), and ONLY if that works
  saves the token to the secrets folder. Prints the account name it connected to. Never
  prints the token.

  Where the token comes from: gumroad.com > Settings > Advanced > Applications > create an
  application (any name, e.g. "Pionir"; any redirect URI) > "Generate access token",
  signed in as the account the Dokaz products are sold from. Gumroad's own app page gives a
  token with the "account" scope, which covers creating products and reading sales.

  Nothing goes on sale because of this: every product still waits for your yes on Discord.
  If Pionir ever says "the Gumroad token was rejected", revoke the old token on Gumroad,
  generate a new one, and run this again.

  After this, check it any time with:  python -m pionir gumroad-check

.PARAMETER TokenFile
  Where the token is kept. Default: $env:USERPROFILE\.pionir\secrets\gumroad-token.txt

.PARAMETER ApiUrl
  The Gumroad API base. Default: https://api.gumroad.com/v2
#>
[CmdletBinding()]
param(
    [string]$TokenFile = "",
    [string]$ApiUrl = "https://api.gumroad.com/v2"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $TokenFile) { $TokenFile = Join-Path $env:USERPROFILE ".pionir\secrets\gumroad-token.txt" }
$ApiUrl = $ApiUrl.TrimEnd('/')
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
# Windows PowerShell 5.1 may default to TLS 1.0, which Gumroad refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

Write-Host ""
Write-Host "Pionir Gumroad products - setup" -ForegroundColor Cyan
Write-Host "  token file : $TokenFile"
Write-Host "  api        : $ApiUrl"
Write-Host ""

# ---- is it even a token? (checked locally; the value is never shown) --------------------
function Get-TokenProblem([string]$t) {
    if ($t.Length -lt 20) {
        return "that is too short to be a Gumroad access token - the paste probably did not go in. In this window, paste with a RIGHT-CLICK; Ctrl+V does not always work in a hidden prompt."
    }
    if ($t.Length -gt 200) {
        return "that is too long to be a Gumroad access token. Copy just the token and try again."
    }
    if ($t -match '^https?:') {
        return "that is a web address, not a token. Copy the access token itself from Gumroad > Settings > Advanced > Applications."
    }
    if ($t -notmatch '^[A-Za-z0-9_\-\.~+/=]+$') {
        return "that has characters a Gumroad token does not have (spaces, quotes or line breaks?). Copy just the token and try again."
    }
    return $null
}

# ---- the token: read hidden ---------------------------------------------------------------
$token = ""
if (Test-Path -LiteralPath $TokenFile) {
    $answer = Read-Host "A Gumroad token is already saved. Replace it? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') {
        Write-Host "  kept the saved token. Check it with: python -m pionir gumroad-check"
        exit 0
    }
}
$secure = Read-Host "Paste the Gumroad access token (input is hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
$token = $token.Trim()
if ($token -match '^(?i)bearer\s+') { $token = ($token -replace '^(?i)bearer\s+', '').Trim() }
if (-not $token) {
    Write-NotOk "no token entered; nothing saved"
    exit 1
}
$problem = Get-TokenProblem $token
if ($problem) {
    Write-NotOk "not saved: $problem"
    $token = $null
    exit 1
}

# ---- ask Gumroad whose account it is ------------------------------------------------------
Write-Host ""
Write-Host "Checking with Gumroad..." -ForegroundColor Cyan
$me = $null
$code = 0
$why = ""
try {
    $headers = @{ "Authorization" = "Bearer $token"; "Accept" = "application/json" }
    $me = Invoke-RestMethod -Uri ($ApiUrl + "/user") -Method Get -Headers $headers `
        -UserAgent "pionir-gumroad-setup/0.1" -TimeoutSec 30
}
catch {
    $response = $_.Exception.Response
    if ($null -ne $response) { $code = [int]$response.StatusCode }
    $why = [string]$_.Exception.Message
    if ($null -ne $_.ErrorDetails -and $_.ErrorDetails.Message) {
        try {
            $body = $_.ErrorDetails.Message | ConvertFrom-Json
            if ($body.PSObject.Properties.Name -contains "message") { $why = [string]$body.message }
            elseif ($body.PSObject.Properties.Name -contains "error") { $why = [string]$body.error }
        }
        catch {
            $why = [string]$_.ErrorDetails.Message
        }
    }
    $why = $why.Replace($token, "<redacted>")
    if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
}
$headers = $null

if ($null -eq $me) {
    if ($code -eq 401 -or $code -eq 403) {
        Write-NotOk "Gumroad rejected the token (HTTP $code). Generate a new one on Gumroad > Settings > Advanced > Applications and run this again. Nothing saved."
    }
    elseif ($code -ge 400 -and $code -lt 500) {
        Write-NotOk "Gumroad refused the check (HTTP $code): $why. Nothing saved."
    }
    else {
        Write-NotOk "could not reach Gumroad to check the token (HTTP $code): $why. Nothing saved."
    }
    $token = $null
    Remove-Variable -Name token -ErrorAction SilentlyContinue
    exit 1
}

# Gumroad answers some refusals with HTTP 200 and success: false.
$success = $false
if ($me.PSObject.Properties.Name -contains "success") { $success = [bool]$me.success }
if (-not $success) {
    $said = ""
    if ($me.PSObject.Properties.Name -contains "message") { $said = ([string]$me.message).Replace($token, "<redacted>") }
    Write-NotOk "Gumroad did not accept the token: $said. Nothing saved."
    $token = $null
    Remove-Variable -Name token -ErrorAction SilentlyContinue
    exit 1
}

$account = ""
if ($me.PSObject.Properties.Name -contains "user" -and $null -ne $me.user) {
    if ($me.user.PSObject.Properties.Name -contains "name") { $account = [string]$me.user.name }
    if (-not $account -and $me.user.PSObject.Properties.Name -contains "user_id") { $account = [string]$me.user.user_id }
}
if (-not $account) {
    Write-NotOk "Gumroad answered without an account; nothing saved."
    $token = $null
    Remove-Variable -Name token -ErrorAction SilentlyContinue
    exit 1
}

# ---- save it, readable only by you -----------------------------------------------------------
$tokenDir = Split-Path -Parent $TokenFile
New-Item -ItemType Directory -Force -Path $tokenDir | Out-Null
[IO.File]::WriteAllText($TokenFile, $token, $utf8NoBom)
$token = $null
Remove-Variable -Name token -ErrorAction SilentlyContinue
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $TokenFile /inheritance:r /grant:r "$($who):(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (could not restrict the token file's permissions; it is saved anyway)" -ForegroundColor Yellow
}

Write-Ok "connected to Gumroad as $account"
Write-Host "  token saved to $TokenFile (not shown)"
Write-Host ""
Write-Host "OK - Gumroad products are set up. Every product still waits for your yes before it goes on sale." -ForegroundColor Green
Write-Host "Check it any time with: python -m pionir gumroad-check"
Write-Host "Once, before the first real product, prove the uploads work (a throwaway draft, never published, deleted at the end):"
Write-Host "  python -m pionir gumroad-check --probe-upload"
exit 0
