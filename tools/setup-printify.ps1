<#
.SYNOPSIS
  One-time setup for Pionir's print-on-demand stream (Printify -> the Etsy shop). Run it yourself.

.DESCRIPTION
  Asks for a Printify personal access token (hidden), asks Printify which stores it can see
  (GET /v1/shops.json), picks the store connected to Etsy (asks if there is more than one),
  and ONLY if that works saves printify.json (the token and the store id) to the secrets
  folder, readable only by you. Never prints the token.

  BEFORE running it:
    - open the Etsy shop and run tools\setup-etsy.ps1 first;
    - in Printify: Add a store > Etsy, and connect it to the same Etsy shop;
    - in Printify: Settings > Orders > set order approval to MANUAL, so no order is sent to
      production (which charges your card) without you looking at it;
    - in Printify: Account > Connections > Generate a personal access token, with the scopes
      shops.read, catalog.read, products.read, products.write, uploads.read, uploads.write,
      orders.read.

  Nothing is made or published because of this: every product and every publish still waits
  for your yes on Discord, and each publish says it costs Etsy's $0.20 listing fee.

.PARAMETER CredentialsFile
  Where the token is kept. Default: $env:USERPROFILE\.pionir\secrets\printify.json
#>
[CmdletBinding()]
param(
    [string]$CredentialsFile = "",
    [string]$ApiUrl = "https://api.printify.com/v1"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

if (-not $CredentialsFile) { $CredentialsFile = Join-Path $env:USERPROFILE ".pionir\secrets\printify.json" }
$ApiUrl = $ApiUrl.TrimEnd('/')
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Ok([string]$text) { Write-Host "  OK      $text" -ForegroundColor Green }
function Write-NotOk([string]$text) { Write-Host "  NOT OK  $text" -ForegroundColor Red }

Write-Host ""
Write-Host "Pionir Printify - setup" -ForegroundColor Cyan
Write-Host "  credentials file : $CredentialsFile"
Write-Host ""

if (Test-Path -LiteralPath $CredentialsFile) {
    $answer = Read-Host "A Printify token is already saved. Replace it? (y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') {
        Write-Host "  kept the saved token."
        exit 0
    }
}

$secure = Read-Host "Paste the Printify personal access token (input is hidden; right-click pastes)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $token = ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)).Trim() }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if ($token.Length -lt 20 -or $token -notmatch '^[A-Za-z0-9_\-\.]+$') {
    Write-NotOk "that does not look like a Printify token (too short, or spaces/quotes in it). Nothing saved."
    exit 1
}

Write-Host "Asking Printify which stores this token can see..." -ForegroundColor Cyan
$shops = $null
try {
    $shops = Invoke-RestMethod -Method Get -Uri "$ApiUrl/shops.json" -TimeoutSec 30 `
        -Headers @{ "Authorization" = "Bearer $token"; "User-Agent" = "Pionir-POD-setup/0.1" }
}
catch {
    $code = 0
    if ($null -ne $_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    if ($code -eq 401 -or $code -eq 403) {
        Write-NotOk "Printify rejected the token (HTTP $code). Generate a new one and run this again. Nothing saved."
    }
    else {
        Write-NotOk "could not reach Printify (HTTP $code). Nothing saved."
    }
    exit 1
}
$etsy = @($shops | Where-Object { [string]$_.sales_channel -eq "etsy" })
if ($etsy.Count -eq 0) {
    Write-NotOk "no Printify store is connected to Etsy. In Printify: Add a store > Etsy, then run this again. Nothing saved."
    exit 1
}
$chosen = $etsy[0]
if ($etsy.Count -gt 1) {
    for ($i = 0; $i -lt $etsy.Count; $i++) { Write-Host ("  [{0}] {1} (id {2})" -f ($i + 1), $etsy[$i].title, $etsy[$i].id) }
    $pick = Read-Host "Which store is the Etsy shop Pionir lists on? (number)"
    $n = 0
    if (-not [int]::TryParse($pick, [ref]$n) -or $n -lt 1 -or $n -gt $etsy.Count) {
        Write-NotOk "not one of the numbers shown. Nothing saved."
        exit 1
    }
    $chosen = $etsy[$n - 1]
}

$dir = Split-Path -Parent $CredentialsFile
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$record = [ordered]@{ token = $token; shop_id = [string]$chosen.id; shop_title = [string]$chosen.title; sales_channel = "etsy" }
[IO.File]::WriteAllText($CredentialsFile, ($record | ConvertTo-Json), $utf8NoBom)
$record = $null
$token = $null
Remove-Variable -Name token, record -ErrorAction SilentlyContinue
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $CredentialsFile /inheritance:r /grant:r "$($who):(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (could not restrict the file's permissions; it is saved anyway)" -ForegroundColor Yellow
}

Write-Ok ("connected to the Printify store '{0}' (id {1}), which publishes to Etsy" -f $chosen.title, $chosen.id)
Write-Host "  token saved to $CredentialsFile (not shown)"
Write-Host ""
Write-Host "Reminder: Printify > Settings > Orders > order approval MANUAL, so production is never charged without you." -ForegroundColor Yellow
Write-Host "OK - the print-on-demand worker starts on its next run. Every product and publish still waits for your yes on Discord." -ForegroundColor Green
exit 0
