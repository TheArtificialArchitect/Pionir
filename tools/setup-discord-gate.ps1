<#
.SYNOPSIS
  One-time setup for Pionir's Discord approval gate. Run it yourself.

.DESCRIPTION
  Asks for the bot token (hidden), saves it to the secrets file without ever
  printing it, asks for the approvals channel id and YOUR Discord user id, saves
  those (not secret) to <state root>\discord\config.json, then checks against
  Discord that the token works, the bot can see the channel, and the user id is
  real. Prints OK / NOT OK for each and overall. Never prints the token.

  Only reactions from the user id you give here can approve or deny anything.
  With no user id, the gate posts approvals but can accept no answers.

  In Discord: Developer Mode on (Settings > Advanced), then right-click the
  channel > Copy Channel ID, and right-click yourself > Copy User ID.
  The bot needs, in that channel: View Channel, Send Messages, Add Reactions,
  Read Message History.

.PARAMETER TokenFile
  Where the token is kept. Default: ~\.pionir\secrets\discord-bot-token.txt

.PARAMETER StateRoot
  Pionir's state root. Default: $env:PIONIR_STATE_ROOT, else ~\.pionir
#>
[CmdletBinding()]
param(
    [string]$TokenFile = "",
    [string]$StateRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $TokenFile) { $TokenFile = Join-Path $HOME ".pionir\secrets\discord-bot-token.txt" }
if (-not $StateRoot) {
    if ($env:PIONIR_STATE_ROOT) { $StateRoot = $env:PIONIR_STATE_ROOT } else { $StateRoot = Join-Path $HOME ".pionir" }
}
$api = "https://discord.com/api/v10"
$configDir = Join-Path $StateRoot "discord"
$configFile = Join-Path $configDir "config.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
# Windows PowerShell 5.1 may default to TLS 1.0, which Discord refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

function Read-DiscordId([string]$label, [string]$current) {
    while ($true) {
        $hint = ""
        if ($current) { $hint = " [Enter keeps $current]" }
        $answer = (Read-Host "$label$hint").Trim()
        if (-not $answer) {
            if ($current) { return $current }
            Write-Host "    A Discord id is required (17-20 digits)." -ForegroundColor Yellow
            continue
        }
        if ($answer -match '^\d{17,20}$') { return $answer }
        Write-Host "    That is not a Discord id (17-20 digits, from 'Copy ID')." -ForegroundColor Yellow
    }
}

# ---- existing settings, if any --------------------------------------------
$oldChannel = ""
$oldUser = ""
if (Test-Path -LiteralPath $configFile) {
    try {
        $old = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
        if ($old.PSObject.Properties.Name -contains "channel_id") { $oldChannel = [string]$old.channel_id }
        if ($old.PSObject.Properties.Name -contains "user_id") { $oldUser = [string]$old.user_id }
    }
    catch {
        Write-Host "Existing $configFile is unreadable; it will be replaced." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Pionir Discord approval gate - setup" -ForegroundColor Cyan
Write-Host "  token file : $TokenFile"
Write-Host "  settings   : $configFile"
Write-Host ""

# ---- the token: read hidden, written without echo ---------------------------
$token = ""
$replace = $true
if (Test-Path -LiteralPath $TokenFile) {
    $answer = Read-Host "A token is already saved. Replace it? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') { $replace = $false }
}
if ($replace) {
    $secure = Read-Host "Paste the bot token (input is hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $token = ($token.Trim() -replace '^(?i)bot\s+', '')
    if (-not $token) {
        Write-NotOk "no token entered; nothing saved"
        exit 1
    }
    $tokenDir = Split-Path -Parent $TokenFile
    New-Item -ItemType Directory -Force -Path $tokenDir | Out-Null
    [IO.File]::WriteAllText($TokenFile, $token, $utf8NoBom)
    # Only you can read it.
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $TokenFile /inheritance:r /grant:r "$($me):(R,W)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  (could not restrict the token file's permissions; it is saved anyway)" -ForegroundColor Yellow
    }
    Write-Host "  token saved (not shown)"
}
else {
    $token = ([IO.File]::ReadAllText($TokenFile)).Trim() -replace '^(?i)bot\s+', ''
}

# ---- channel and owner -------------------------------------------------------
$channel = Read-DiscordId "Approvals channel id" $oldChannel
$user = Read-DiscordId "YOUR Discord user id (only your reactions count)" $oldUser

New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$config = [ordered]@{ channel_id = $channel; user_id = $user; token_file = $TokenFile }
[IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json), $utf8NoBom)
Write-Host "  settings saved to $configFile"

# ---- verify against Discord -----------------------------------------------------
$headers = @{ "Authorization" = "Bot $token"; "User-Agent" = "DiscordBot (pionir-setup, 0.1)" }

function Invoke-Discord([string]$path) {
    try {
        $body = Invoke-RestMethod -Uri ($api + $path) -Headers $headers -Method Get -TimeoutSec 20
        return @{ ok = $true; code = 200; body = $body; why = "" }
    }
    catch {
        $code = 0
        $response = $_.Exception.Response
        if ($null -ne $response) { $code = [int]$response.StatusCode }
        $why = [string]$_.Exception.Message
        if ($token) { $why = $why.Replace($token, "<redacted>") }
        return @{ ok = $false; code = $code; body = $null; why = $why }
    }
}

Write-Host ""
Write-Host "Checking with Discord..." -ForegroundColor Cyan
$allOk = $true
$botId = ""

$r = Invoke-Discord "/users/@me"
if ($r.ok) {
    $botId = [string]$r.body.id
    Write-Ok "token works: the bot is $($r.body.username)"
}
elseif ($r.code -eq 401) {
    Write-NotOk "Discord rejected the token (401). Reset it in the Developer Portal and run this again."
    $allOk = $false
}
else {
    Write-NotOk "could not reach Discord to check the token (HTTP $($r.code)): $($r.why)"
    $allOk = $false
}

if ($botId) {
    $r = Invoke-Discord "/channels/$channel"
    if ($r.ok) {
        Write-Ok "the bot can see channel #$($r.body.name)"
    }
    else {
        $allOk = $false
        if ($r.code -eq 403) {
            Write-NotOk "the bot cannot see channel $channel (403). Add it to the server and give it View Channel there."
        }
        elseif ($r.code -eq 404) {
            Write-NotOk "no channel $channel that this bot can find (404). Check the id."
        }
        else {
            Write-NotOk "channel check failed (HTTP $($r.code)): $($r.why)"
        }
        $invite = "https://discord.com/oauth2/authorize?client_id=$botId&scope=bot&permissions=68672"
        Write-Host "          Invite link (View Channel, Send Messages, Add Reactions, Read Message History):"
        Write-Host "          $invite"
    }

    $r = Invoke-Discord "/users/$user"
    if ($r.ok) {
        Write-Ok "owner is $($r.body.username) - only their reactions will count"
    }
    elseif ($r.code -eq 404) {
        Write-NotOk "no Discord user $user (404). Copy your own id again."
        $allOk = $false
    }
    else {
        Write-NotOk "could not check user $user (HTTP $($r.code)): $($r.why)"
        $allOk = $false
    }
}

$token = $null
$headers = $null
Remove-Variable -Name token, headers -ErrorAction SilentlyContinue

Write-Host ""
if ($allOk) {
    Write-Host "OK - the Discord gate is set up. Restart Pionir to pick it up." -ForegroundColor Green
    exit 0
}
Write-Host "NOT OK - fix the items above and run this again. Approvals still work from the phone meanwhile." -ForegroundColor Red
exit 1
