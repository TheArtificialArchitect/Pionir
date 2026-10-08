<#
.SYNOPSIS
  One-time setup for Pionir's Chrome Web Store updates, and recording an item you created.
  Run it yourself.

.DESCRIPTION
  Mode 1 (no -Slug): the OAuth credentials. You need, once:
    1. A Chrome Web Store developer account (the $5 one-time fee), and its Publisher ID
       (developer dashboard > Account).
    2. A Google Cloud project with the "Chrome Web Store API" enabled, and an OAuth client
       of type "Desktop app" (APIs & Services > Credentials > Create credentials > OAuth
       client ID). Copy its client ID and client secret. On the OAuth consent screen, add
       your own Google account as a test user.
  The script asks for the Publisher ID, the client ID and the client secret (hidden), prints
  a Google sign-in address for you to open, and asks you to paste back the address your
  browser lands on afterwards (it starts with http://127.0.0.1:8799 and the page itself will
  not load - that is expected). It exchanges that one-time code for a refresh token, checks
  it (and, when you have recorded an item, reads that item's status - a read only), and ONLY
  then saves everything to the secrets folder, readable only by you. Nothing is printed.

  Mode 2 (-Slug <slug> -ItemId <id>): after you created an extension's item BY HAND in the
  developer dashboard (the API cannot create one) from the pack Pionir prepared, record which
  item it is, so Pionir may upload that extension's next versions - each only on your yes.
  When the credentials are set up, the item's status is read once to check the id.

.PARAMETER Slug
  Mode 2: the extension's slug, as Pionir's card named it.

.PARAMETER ItemId
  Mode 2: the 32-letter item id from the developer dashboard.

.PARAMETER CredentialsFile
  Default: $env:USERPROFILE\.pionir\secrets\chrome-webstore.json
#>
[CmdletBinding()]
param(
    [string]$Slug = "",
    [string]$ItemId = "",
    [string]$CredentialsFile = "",
    [string]$ApiUrl = "https://chromewebstore.googleapis.com"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $CredentialsFile) { $CredentialsFile = Join-Path $env:USERPROFILE ".pionir\secrets\chrome-webstore.json" }
$Root = $env:PIONIR_MARKETPLACES_DIR
if (-not $Root) { $Root = Join-Path $env:USERPROFILE ".pionir\marketplaces" }
$ItemsFile = Join-Path $Root "chrome-items.json"
$Redirect = "http://127.0.0.1:8799"
$Scope = "https://www.googleapis.com/auth/chromewebstore"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

function Read-Hidden([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)).Trim() }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Restrict([string]$path) {
    $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $path /inheritance:r /grant:r "$($who):(R,W)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  (could not restrict $path's permissions; it is saved anyway)" -ForegroundColor Yellow
    }
}

function Get-AccessToken($creds) {
    $body = @{ client_id = $creds.client_id; client_secret = $creds.client_secret;
               refresh_token = $creds.refresh_token; grant_type = "refresh_token" }
    $answer = Invoke-RestMethod -Uri "https://oauth2.googleapis.com/token" -Method Post -Body $body -TimeoutSec 30
    return [string]$answer.access_token
}

function Read-ItemStatus($creds, [string]$item) {
    $access = Get-AccessToken $creds
    $publisher = [Uri]::EscapeDataString([string]$creds.publisher_id)
    $uri = "$ApiUrl/v2/publishers/$publisher/items/$($item):fetchStatus"
    return Invoke-RestMethod -Uri $uri -Method Get -Headers @{ "Authorization" = "Bearer $access" } -TimeoutSec 30
}

Write-Host ""
Write-Host "Pionir Chrome Web Store updates - setup" -ForegroundColor Cyan

# ---- mode 2: record a hand-made item ----------------------------------------------------------
if ($Slug -or $ItemId) {
    if ($Slug -notmatch '^[a-z0-9][a-z0-9-]{2,39}$') { Write-NotOk "-Slug must be 3-40 of a-z, 0-9 and -"; exit 1 }
    if ($ItemId -notmatch '^[a-p]{32}$') { Write-NotOk "-ItemId must be the 32-letter item id (letters a to p)"; exit 1 }
    if (Test-Path -LiteralPath $CredentialsFile) {
        try {
            $creds = Get-Content -LiteralPath $CredentialsFile -Raw | ConvertFrom-Json
            $status = Read-ItemStatus $creds $ItemId
            Write-Ok "the Chrome Web Store knows item $ItemId (a read only)"
        }
        catch {
            Write-NotOk "could not read item $ItemId with your credentials: $($_.Exception.Message). Nothing recorded."
            exit 1
        }
    }
    else {
        Write-Host "  (no credentials yet, so the item id is not checked; run this script without -Slug first)" -ForegroundColor Yellow
    }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $items = @{}
    if (Test-Path -LiteralPath $ItemsFile) {
        $doc = Get-Content -LiteralPath $ItemsFile -Raw | ConvertFrom-Json
        foreach ($p in $doc.PSObject.Properties) { $items[$p.Name] = [string]$p.Value }
    }
    $items[$Slug] = $ItemId
    [IO.File]::WriteAllText($ItemsFile, ($items | ConvertTo-Json), $utf8NoBom)
    Write-Ok "recorded $Slug -> $ItemId in $ItemsFile"
    Write-Host "Its next version is uploaded and submitted for review only when you approve it."
    exit 0
}

# ---- mode 1: the OAuth credentials ---------------------------------------------------------------
Write-Host "  credentials file : $CredentialsFile"
Write-Host ""
if (Test-Path -LiteralPath $CredentialsFile) {
    $answer = Read-Host "Chrome Web Store credentials are already saved. Replace them? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') { Write-Host "  kept the saved credentials."; exit 0 }
}
$publisher = (Read-Host "Publisher ID (developer dashboard > Account)").Trim()
$clientId = (Read-Host "OAuth client ID (ends with .apps.googleusercontent.com)").Trim()
$clientSecret = Read-Hidden "OAuth client secret (input is hidden)"
if (-not $publisher -or $publisher -notmatch '^[A-Za-z0-9._@-]{1,120}$') { Write-NotOk "that is not a Publisher ID; nothing saved"; exit 1 }
if ($clientId -notmatch '\.apps\.googleusercontent\.com$') { Write-NotOk "that is not an OAuth client ID; nothing saved"; exit 1 }
if ($clientSecret.Length -lt 10) { Write-NotOk "the client secret is too short - paste with a RIGHT-CLICK; nothing saved"; exit 1 }

$auth = "https://accounts.google.com/o/oauth2/v2/auth?response_type=code" +
        "&client_id=" + [Uri]::EscapeDataString($clientId) +
        "&redirect_uri=" + [Uri]::EscapeDataString($Redirect) +
        "&scope=" + [Uri]::EscapeDataString($Scope) +
        "&access_type=offline&prompt=consent"
Write-Host ""
Write-Host "Open this address in your browser, sign in with the developer account and allow access:" -ForegroundColor Cyan
Write-Host ""
Write-Host $auth
Write-Host ""
Write-Host "Your browser then goes to $Redirect/?code=... and shows an error page - that is expected."
$landed = (Read-Host "Paste the whole address from the browser's address bar").Trim()
$code = ""
if ($landed -match '[?&]code=([^&]+)') { $code = [Uri]::UnescapeDataString($Matches[1]) }
if (-not $code) { Write-NotOk "no ?code= in that address; nothing saved"; exit 1 }

try {
    $body = @{ code = $code; client_id = $clientId; client_secret = $clientSecret;
               redirect_uri = $Redirect; grant_type = "authorization_code" }
    $tokens = Invoke-RestMethod -Uri "https://oauth2.googleapis.com/token" -Method Post -Body $body -TimeoutSec 30
}
catch {
    Write-NotOk "Google refused the code: $($_.Exception.Message). Nothing saved."
    exit 1
}
$refresh = ""
if ($tokens.PSObject.Properties.Name -contains "refresh_token") { $refresh = [string]$tokens.refresh_token }
if (-not $refresh) {
    Write-NotOk "Google gave no refresh token (remove the app's access in your Google account and run this again). Nothing saved."
    exit 1
}
$creds = [ordered]@{ client_id = $clientId; client_secret = $clientSecret;
                     refresh_token = $refresh; publisher_id = $publisher }

# ---- verify with one read-only call ------------------------------------------------------------
try {
    $access = Get-AccessToken ([pscustomobject]$creds)
    if (-not $access) { throw "no access token" }
    Write-Ok "the refresh token works (a new access token was issued)"
    if (Test-Path -LiteralPath $ItemsFile) {
        $doc = Get-Content -LiteralPath $ItemsFile -Raw | ConvertFrom-Json
        $first = @($doc.PSObject.Properties)[0]
        if ($null -ne $first) {
            $null = Read-ItemStatus ([pscustomobject]$creds) ([string]$first.Value)
            Write-Ok "read the status of $($first.Name) (a read only)"
        }
    }
}
catch {
    Write-NotOk "the credentials did not work: $($_.Exception.Message). Nothing saved."
    exit 1
}

$dir = Split-Path -Parent $CredentialsFile
New-Item -ItemType Directory -Force -Path $dir | Out-Null
[IO.File]::WriteAllText($CredentialsFile, ($creds | ConvertTo-Json), $utf8NoBom)
Restrict $CredentialsFile
$clientSecret = $null
$refresh = $null
$creds = $null
Write-Ok "saved to $CredentialsFile (not shown)"
Write-Host ""
Write-Host "OK - Chrome Web Store updates are set up. Each upload still waits for your yes." -ForegroundColor Green
Write-Host "After you create an extension's item by hand, record it with:"
Write-Host "  .\tools\setup-chrome-webstore.ps1 -Slug <slug> -ItemId <item id>"
exit 0
