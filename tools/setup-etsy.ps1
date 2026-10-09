<#
.SYNOPSIS
  One-time setup for Pionir's Etsy streams (digital downloads + print on demand). Run it yourself.

.DESCRIPTION
  Connects Pionir to YOUR Etsy shop with Etsy's OAuth 2.0 (PKCE) sign-in:

  1. asks for the Etsy app's keystring and shared secret (the secret is hidden);
  2. opens Etsy's consent page in your browser (you sign in and click "Allow access");
  3. catches Etsy's answer on this computer only (http://localhost:3003/oauth/redirect);
  4. trades the one-time code for an access token and a refresh token;
  5. asks Etsy which shop is yours (GET /v3/application/users/{user_id}/shops);
  6. ONLY if all of that worked, saves etsy.json in the secrets folder, readable only by you.

  Never prints a secret or a token. Pionir refreshes the access token itself (it lives an
  hour; the refresh token 90 days, renewed on every refresh). If Pionir ever says "Etsy
  refused the credentials" or "could not be refreshed", run this again.

  BEFORE running it:
    - open the Etsy shop (Etsy charges a one-time shop opening fee) and finish its setup;
    - create an app at https://www.etsy.com/developers/your-apps ("Create a New App");
    - on the app, add the callback URL exactly:  http://localhost:3003/oauth/redirect
    - copy the app's KEYSTRING and SHARED SECRET from that page.

  Nothing is listed because of this: every Etsy listing still waits for your yes on Discord,
  and each one says it costs Etsy's $0.20 listing fee.

.PARAMETER CredentialsFile
  Where the credentials are kept. Default: $env:USERPROFILE\.pionir\secrets\etsy.json

.PARAMETER Port
  The local port Etsy's answer comes back to. Default 3003 (it must match the callback URL
  registered on the app).
#>
[CmdletBinding()]
param(
    [string]$CredentialsFile = "",
    [int]$Port = 3003
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $CredentialsFile) { $CredentialsFile = Join-Path $env:USERPROFILE ".pionir\secrets\etsy.json" }
$Redirect = "http://localhost:$Port/oauth/redirect"
$ApiBase = "https://api.etsy.com/v3"
$Scopes = "listings_r listings_w transactions_r shops_r"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

function ConvertTo-Base64Url([byte[]]$bytes) {
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-RandomBytes([int]$count) {
    $bytes = New-Object byte[] $count
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ,$bytes
}

function Read-Hidden([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)).Trim() }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Get-EtsyError($err) {
    $why = [string]$err.Exception.Message
    if ($null -ne $err.ErrorDetails -and $err.ErrorDetails.Message) {
        try {
            $body = $err.ErrorDetails.Message | ConvertFrom-Json
            if ($body.PSObject.Properties.Name -contains "error_description") { $why = [string]$body.error_description }
            elseif ($body.PSObject.Properties.Name -contains "error") { $why = [string]$body.error }
        }
        catch { $why = [string]$err.ErrorDetails.Message }
    }
    return $why
}

Write-Host ""
Write-Host "Pionir Etsy - setup" -ForegroundColor Cyan
Write-Host "  credentials file : $CredentialsFile"
Write-Host "  callback URL     : $Redirect   (must be on your Etsy app exactly like this)"
Write-Host ""

if (Test-Path -LiteralPath $CredentialsFile) {
    $answer = Read-Host "Etsy credentials are already saved. Replace them? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') {
        Write-Host "  kept the saved credentials."
        exit 0
    }
}

# ---- the app's key and secret ----------------------------------------------------------
$keystring = (Read-Host "Paste the app's KEYSTRING (from etsy.com/developers/your-apps)").Trim()
if ($keystring -notmatch '^[A-Za-z0-9]{10,64}$') {
    Write-NotOk "that does not look like an Etsy keystring (letters and digits, from the app page). Nothing saved."
    exit 1
}
$secret = Read-Hidden "Paste the app's SHARED SECRET (input is hidden; right-click pastes)"
if ($secret -notmatch '^[A-Za-z0-9]{6,64}$') {
    Write-NotOk "that does not look like an Etsy shared secret (letters and digits). Nothing saved."
    exit 1
}
$apiKey = "${keystring}:${secret}"

# ---- PKCE: a one-time verifier, its challenge, and a state value ------------------------
$verifier = ConvertTo-Base64Url (New-RandomBytes 48)
$sha = [Security.Cryptography.SHA256]::Create()
try { $challenge = ConvertTo-Base64Url ($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($verifier))) }
finally { $sha.Dispose() }
$state = ConvertTo-Base64Url (New-RandomBytes 24)

$authUrl = "https://www.etsy.com/oauth/connect?response_type=code" +
    "&redirect_uri=" + [Uri]::EscapeDataString($Redirect) +
    "&scope=" + [Uri]::EscapeDataString($Scopes) +
    "&client_id=" + [Uri]::EscapeDataString($keystring) +
    "&state=" + $state +
    "&code_challenge=" + $challenge +
    "&code_challenge_method=S256"

# ---- listen on this computer only, then send the browser to Etsy -------------------------
$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:$Port/")
try { $listener.Start() }
catch {
    Write-NotOk "could not listen on http://localhost:$Port/ ($($_.Exception.Message)). Is something else using port $Port? Nothing saved."
    exit 1
}
$code = $null
$failure = $null
try {
    Write-Host ""
    Write-Host "Opening Etsy in your browser. Sign in to the shop's account and click 'Allow access'." -ForegroundColor Cyan
    Write-Host "  If no browser opens, paste this into one:"
    Write-Host "  $authUrl"
    Start-Process $authUrl
    $pending = $listener.BeginGetContext($null, $null)
    if (-not $pending.AsyncWaitHandle.WaitOne([TimeSpan]::FromMinutes(5))) {
        $failure = "no answer from Etsy within 5 minutes"
    }
    else {
        $context = $listener.EndGetContext($pending)
        $query = $context.Request.QueryString
        $html = "<html><body style='font-family:sans-serif'><h2>Pionir: you can close this tab and go back to PowerShell.</h2></body></html>"
        if ($query["state"] -ne $state) {
            $failure = "the answer did not carry this setup's state value (refused, for safety)"
            $html = "<html><body><h2>Pionir: refused - the state did not match. Run the setup again.</h2></body></html>"
        }
        elseif ($query["error"]) {
            $failure = "Etsy said: " + $query["error"] + " " + $query["error_description"]
        }
        else {
            $code = $query["code"]
            if (-not $code) { $failure = "Etsy's answer carried no code" }
        }
        $bytes = [Text.Encoding]::UTF8.GetBytes($html)
        $context.Response.ContentType = "text/html; charset=utf-8"
        # Without a length the response goes out chunked and the listener closes right after, so
        # the browser often showed a blank page instead of this message.
        $context.Response.ContentLength64 = $bytes.Length
        $context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
        $context.Response.Close()
    }
}
finally {
    $listener.Stop()
    $listener.Close()
}
if ($failure) {
    Write-NotOk "$failure. Nothing saved."
    exit 1
}

# ---- trade the code for tokens -------------------------------------------------------------
Write-Host "Getting the tokens from Etsy..." -ForegroundColor Cyan
$tokens = $null
try {
    $tokens = Invoke-RestMethod -Method Post -Uri "$ApiBase/public/oauth/token" -TimeoutSec 30 `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{ grant_type = "authorization_code"; client_id = $keystring; redirect_uri = $Redirect; code = $code; code_verifier = $verifier }
}
catch {
    Write-NotOk ("Etsy refused the code: " + (Get-EtsyError $_) + ". Nothing saved.")
    exit 1
}
$access = [string]$tokens.access_token
$refresh = [string]$tokens.refresh_token
if (-not $access -or -not $refresh -or $access -notmatch '^(\d+)\.') {
    Write-NotOk "Etsy answered without usable tokens. Nothing saved."
    exit 1
}
$userId = $Matches[1]
$lifetime = 3600
if ($tokens.PSObject.Properties.Name -contains "expires_in") { $lifetime = [int]$tokens.expires_in }

# ---- whose shop is it? -----------------------------------------------------------------------
$shop = $null
try {
    $shop = Invoke-RestMethod -Method Get -Uri "$ApiBase/application/users/$userId/shops" -TimeoutSec 30 `
        -Headers @{ "x-api-key" = $apiKey; "Authorization" = "Bearer $access" }
}
catch {
    Write-NotOk ("Etsy would not say which shop is yours: " + (Get-EtsyError $_) + ". Is the shop open? Nothing saved.")
    exit 1
}
$shopId = ""
$shopName = ""
if ($null -ne $shop -and $shop.PSObject.Properties.Name -contains "shop_id") {
    $shopId = [string]$shop.shop_id
    $shopName = [string]$shop.shop_name
}
if ($shopId -notmatch '^\d+$') {
    Write-NotOk "this Etsy account has no shop yet. Open the shop on Etsy first, then run this again. Nothing saved."
    exit 1
}

# ---- save it, readable only by you ---------------------------------------------------------------
$dir = Split-Path -Parent $CredentialsFile
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$record = [ordered]@{
    keystring = $keystring; shared_secret = $secret; shop_id = $shopId; shop_name = $shopName
    user_id = $userId; access_token = $access; refresh_token = $refresh
    expires_at = ($now + $lifetime); refreshed_at = $now
}
[IO.File]::WriteAllText($CredentialsFile, ($record | ConvertTo-Json), $utf8NoBom)
$record = $null
$secret = $null
$access = $null
$refresh = $null
Remove-Variable -Name secret, access, refresh, record, apiKey -ErrorAction SilentlyContinue
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $CredentialsFile /inheritance:r /grant:r "$($who):(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (could not restrict the file's permissions; it is saved anyway)" -ForegroundColor Yellow
}

Write-Ok "connected to the Etsy shop '$shopName' (shop id $shopId)"
Write-Host "  credentials saved to $CredentialsFile (not shown)"
Write-Host ""
Write-Host "OK - the Etsy workers start on their next run. Every listing still waits for your yes on Discord." -ForegroundColor Green
exit 0
