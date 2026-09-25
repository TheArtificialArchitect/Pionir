<#
.SYNOPSIS
  One-time setup for Pionir's Instagram posting. Run it yourself.

.DESCRIPTION
  Asks for the Instagram long-lived access token (hidden), checks its shape locally,
  asks Instagram who it belongs to (GET /me?fields=user_id,username), and ONLY if that
  works saves instagram.json (the token, user_id, username and refreshed_at = now) to
  the secrets folder. Prints the account it connected to. Never prints the token.

  Pionir refreshes the token itself when it is over 7 days old (on the way to a post;
  nothing runs in the background). If it ever says "the Instagram token was rejected or
  has expired", run this again with a new token.

  Where the token comes from (Instagram API with Instagram Login): Meta for Developers >
  your app > Instagram > API setup with Instagram login > Generate access tokens, for the
  Dokaz account. It is a long string that usually starts with IG. A token that starts
  with EAA is a Facebook Login token and will not work here.

  After this, check it any time with:  python -m pionir instagram-check

.PARAMETER TokenFile
  Where the token is kept. Default: $env:USERPROFILE\.pionir\secrets\instagram.json

.PARAMETER GraphUrl
  The Instagram Graph API base. Default: https://graph.instagram.com/v25.0
#>
[CmdletBinding()]
param(
    [string]$TokenFile = "",
    [string]$GraphUrl = "https://graph.instagram.com/v25.0"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $TokenFile) { $TokenFile = Join-Path $env:USERPROFILE ".pionir\secrets\instagram.json" }
$GraphUrl = $GraphUrl.TrimEnd('/')
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
# Windows PowerShell 5.1 may default to TLS 1.0, which Meta refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

Write-Host ""
Write-Host "Pionir Instagram posting - setup" -ForegroundColor Cyan
Write-Host "  token file : $TokenFile"
Write-Host "  graph api  : $GraphUrl"
Write-Host ""

# ---- is it even an Instagram token? (checked locally; the value is never shown) -------
function Get-TokenProblem([string]$t) {
    if ($t.Length -lt 40) {
        return "that is too short to be an access token - the paste probably did not go in. In this window, paste with a RIGHT-CLICK; Ctrl+V does not always work in a hidden prompt."
    }
    if ($t -match '^\d+$') {
        return "that is an id (all digits), not an access token. Generate a token on the app's Instagram > API setup page."
    }
    if ($t -match '^EAA') {
        return "that is a Facebook Login token (it starts with EAA). Pionir posts with Instagram Login: generate the token on the app's Instagram > API setup with Instagram login page."
    }
    if ($t -notmatch '^[A-Za-z0-9_\-\.|]+$') {
        return "that has characters an access token does not have (spaces, quotes or line breaks?). Copy just the token and try again."
    }
    return $null
}

# ---- the token: read hidden ---------------------------------------------------------
$token = ""
if (Test-Path -LiteralPath $TokenFile) {
    $answer = Read-Host "An Instagram token is already saved. Replace it? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') {
        Write-Host "  kept the saved token. Check it with: python -m pionir instagram-check"
        exit 0
    }
}
$secure = Read-Host "Paste the Instagram access token (input is hidden)" -AsSecureString
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
if ($token -notmatch '^IG') {
    Write-Host "  (it does not start with IG, as Instagram Login tokens usually do - checking it with Instagram anyway)" -ForegroundColor Yellow
}

# ---- ask Instagram who it belongs to -----------------------------------------------------
Write-Host ""
Write-Host "Checking with Instagram..." -ForegroundColor Cyan
$uri = $GraphUrl + "/me?fields=user_id,username&access_token=" + [Uri]::EscapeDataString($token)
$me = $null
$code = 0
$why = ""
$graphCode = $null
try {
    $me = Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec 30
}
catch {
    $response = $_.Exception.Response
    if ($null -ne $response) { $code = [int]$response.StatusCode }
    $why = [string]$_.Exception.Message
    if ($null -ne $_.ErrorDetails -and $_.ErrorDetails.Message) {
        try {
            $body = $_.ErrorDetails.Message | ConvertFrom-Json
            if ($null -ne $body.error) {
                $why = [string]$body.error.message
                $graphCode = $body.error.code
            }
        }
        catch {
            $why = [string]$_.ErrorDetails.Message
        }
    }
    $why = $why.Replace($token, "<redacted>").Replace([Uri]::EscapeDataString($token), "<redacted>")
}
$uri = $null

if ($null -eq $me) {
    if ($graphCode -eq 190) {
        Write-NotOk "Instagram rejected the token (code 190: invalid or expired). Generate a new one and run this again. Nothing saved."
    }
    elseif ($code -ge 400 -and $code -lt 500) {
        Write-NotOk "Instagram refused the check (HTTP $code): $why. Nothing saved."
    }
    else {
        Write-NotOk "could not reach Instagram to check the token (HTTP $code): $why. Nothing saved."
    }
    $token = $null
    Remove-Variable -Name token -ErrorAction SilentlyContinue
    exit 1
}

$userId = ""
if ($me.PSObject.Properties.Name -contains "user_id") { $userId = [string]$me.user_id }
if (-not $userId -and ($me.PSObject.Properties.Name -contains "id")) { $userId = [string]$me.id }
$username = ""
if ($me.PSObject.Properties.Name -contains "username") { $username = [string]$me.username }
if ($userId -notmatch '^\d+$') {
    Write-NotOk "Instagram answered without a user id; nothing saved."
    $token = $null
    Remove-Variable -Name token -ErrorAction SilentlyContinue
    exit 1
}

# ---- save it, readable only by you ---------------------------------------------------------
$tokenDir = Split-Path -Parent $TokenFile
New-Item -ItemType Directory -Force -Path $tokenDir | Out-Null
$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$record = [ordered]@{ access_token = $token; user_id = $userId; username = $username; refreshed_at = $now }
[IO.File]::WriteAllText($TokenFile, ($record | ConvertTo-Json), $utf8NoBom)
$record = $null
$token = $null
Remove-Variable -Name token, record -ErrorAction SilentlyContinue
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $TokenFile /inheritance:r /grant:r "$($who):(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (could not restrict the token file's permissions; it is saved anyway)" -ForegroundColor Yellow
}

Write-Ok "connected to Instagram as @$username (user id $userId)"
Write-Host "  token saved to $TokenFile (not shown)"
Write-Host ""
Write-Host "OK - Instagram posting is set up. Every post still waits for your yes." -ForegroundColor Green
Write-Host "Check it any time with: python -m pionir instagram-check"
exit 0
