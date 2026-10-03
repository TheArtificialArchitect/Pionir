<#
.SYNOPSIS
  One-time YouTube consent for the Pionir video uploader. Run it yourself, in a visible window.

.DESCRIPTION
  Reads %USERPROFILE%\.pionir\secrets\youtube-client.json (your Google desktop OAuth client:
  {"client_id": "...", "client_secret": "..."}), opens Google's consent page in your browser,
  catches the answer on a loopback port that exists only until you finish, and writes
  %USERPROFILE%\.pionir\secrets\youtube-token.json ({"refresh_token": "..."}).

  The only permission asked for is "upload videos to your YouTube account". Nothing here is
  installed, scheduled or left running: when the script ends, nothing of it remains. No token,
  secret or code is ever printed. Ctrl+C or closing the window cancels it with nothing written.

  Steps before this: docs\VIDEO_SETUP_FOR_IAN.md.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\video-youtube-consent.ps1
#>
[CmdletBinding()]
param(
  [string]$SecretsDir = (Join-Path $env:USERPROFILE '.pionir\secrets'),
  [int]$TimeoutMinutes = 5
)

$ErrorActionPreference = 'Stop'
$Scope = 'https://www.googleapis.com/auth/youtube.upload'
$ClientFile = Join-Path $SecretsDir 'youtube-client.json'
$TokenFile = Join-Path $SecretsDir 'youtube-token.json'

function Fail([string]$Message) {
  Write-Host "ERROR: $Message" -ForegroundColor Red
  exit 1
}

function New-UrlSafe([byte[]]$Bytes) {
  [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path -LiteralPath $ClientFile)) {
  Fail "Missing $ClientFile. Create it as described in docs\VIDEO_SETUP_FOR_IAN.md (step 4)."
}
try {
  $client = Get-Content -LiteralPath $ClientFile -Raw | ConvertFrom-Json
} catch {
  Fail "$ClientFile is not valid JSON."
}
if (-not $client.client_id -or -not $client.client_secret) {
  Fail "$ClientFile needs both client_id and client_secret."
}
if (Test-Path -LiteralPath $TokenFile) {
  Write-Host "A token file already exists at $TokenFile."
  Write-Host 'Continuing replaces it. Press Ctrl+C now to keep it, or Enter to continue.'
  [void][Console]::ReadLine()
}

# PKCE: a one-use verifier that only this window knows, so a stolen code is useless.
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$buf = New-Object byte[] 48
$rng.GetBytes($buf)
$verifier = New-UrlSafe $buf
$sha = [Security.Cryptography.SHA256]::Create()
$challenge = New-UrlSafe ($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($verifier)))
$sbuf = New-Object byte[] 16
$rng.GetBytes($sbuf)
$state = New-UrlSafe $sbuf

# A free loopback port, held only until you finish.
$probe = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
$probe.Start()
$port = $probe.LocalEndpoint.Port
$probe.Stop()
$redirect = "http://127.0.0.1:$port/"

$listener = New-Object Net.HttpListener
$listener.Prefixes.Add($redirect)
$listener.Start()
try {
  $query = @(
    'client_id=' + [Uri]::EscapeDataString($client.client_id),
    'redirect_uri=' + [Uri]::EscapeDataString($redirect),
    'response_type=code',
    'scope=' + [Uri]::EscapeDataString($Scope),
    'access_type=offline',
    'prompt=consent',
    'state=' + $state,
    'code_challenge=' + $challenge,
    'code_challenge_method=S256'
  ) -join '&'
  $url = "https://accounts.google.com/o/oauth2/v2/auth?$query"

  Write-Host ''
  Write-Host 'Opening Google in your browser. Sign in as the account that owns the channel,'
  Write-Host 'and approve "Upload videos to your YouTube account". Nothing else is requested.'
  Write-Host "(If no browser opens, copy this address into one: it carries no secret.)"
  Write-Host $url
  Write-Host ''
  Start-Process $url

  $pending = $listener.GetContextAsync()
  if (-not $pending.Wait([TimeSpan]::FromMinutes($TimeoutMinutes))) {
    Fail "No answer within $TimeoutMinutes minutes. Nothing was written."
  }
  $ctx = $pending.Result
  $params = [Web.HttpUtility]::ParseQueryString($ctx.Request.Url.Query)
  $reply = 'Done. You can close this tab and return to the PowerShell window.'
  $code = $null
  if ($params['state'] -ne $state) {
    $reply = 'This answer did not match the request that was started. Nothing was done.'
  } elseif ($params['error']) {
    $reply = 'Google reported: ' + $params['error'] + '. Nothing was done.'
  } else {
    $code = $params['code']
  }
  $bytes = [Text.Encoding]::UTF8.GetBytes("<html><body><p>$reply</p></body></html>")
  $ctx.Response.ContentType = 'text/html; charset=utf-8'
  $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
  $ctx.Response.Close()
  if (-not $code) { Fail $reply }
} finally {
  $listener.Stop()
  $listener.Close()
}

try {
  $answer = Invoke-RestMethod -Method Post -Uri 'https://oauth2.googleapis.com/token' -Body @{
    client_id = $client.client_id
    client_secret = $client.client_secret
    code = $code
    code_verifier = $verifier
    grant_type = 'authorization_code'
    redirect_uri = $redirect
  }
} catch {
  Fail 'Google refused the code exchange (check the client id and secret).'
}
if (-not $answer.refresh_token) {
  Fail ('Google returned no refresh token. Remove the app at https://myaccount.google.com/permissions ' +
        'and run this again.')
}

New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null
$json = @{ refresh_token = $answer.refresh_token } | ConvertTo-Json
[IO.File]::WriteAllText($TokenFile, $json, (New-Object Text.UTF8Encoding($false)))
# Only you may read it.
& icacls.exe $TokenFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

Write-Host ''
Write-Host "Saved the refresh token to $TokenFile (not shown here)." -ForegroundColor Green
Write-Host 'Nothing is running in the background. The uploader still sends only approved videos,'
Write-Host 'only from a niche marked live, and only as Private.'
