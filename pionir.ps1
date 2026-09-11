# Pionir launcher. Foreground only: the brain is awake while this window is open.
# Nothing here installs a task, a service, a Run key or a Startup item.
#
#   .\pionir.ps1                 wake the stack and open the dashboard
#   .\pionir.ps1 -NoVoice        don't wake Galatea; just Pionir and whoever's already up
#   .\pionir.ps1 -NoBrowser      don't open a browser (the URL is printed)
#   .\pionir.ps1 -Port 8781      a different port
#   .\pionir.ps1 -Stop           stop a running dashboard, from anywhere
#
# "Whole stack, one launch": this starts Pionir's dashboard and, unless -NoVoice,
# wakes Galatea in her OWN window (her galatea.ps1) if she is not already up, so
# she stays a process you can see and close. Atani, Daedalus and Melete are
# reached where they run; the dashboard shows each one's health. It does not
# start Theo's backend - Daedalus and Melete show unavailable until you do.
param(
    [switch]$NoVoice,
    [switch]$NoBrowser,
    [switch]$Stop,
    [int]$Port = 8780
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Test-Port([int]$p) {
    try { (New-Object Net.Sockets.TcpClient).Connect("127.0.0.1", $p); return $true }
    catch { return $false }
}

if ($Stop) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($c) { Stop-Process -Id $c.OwningProcess -Force; Write-Host "stopped the dashboard on $Port." }
    else { Write-Host "nothing is listening on $Port." }
    exit 0
}

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$env:PYTHONPATH = Join-Path $root "src"

# Ollama is what every specialist's model runs on; a down daemon is not fatal to
# the dashboard but every routed turn would fail, so say so plainly.
if (-not (Test-Port 11434)) {
    Write-Host "  ! Ollama is not answering on 127.0.0.1:11434 - start it, or routed turns will fail." -ForegroundColor Yellow
}

# The voice. Wake her own window if she is not already up, then point Pionir at her.
$galatea = Join-Path (Split-Path -Parent $root) "Galatea\galatea.ps1"
if (-not $NoVoice) {
    $env:PIONIR_GALATEA_URL = "http://127.0.0.1:8799"
    if (-not (Test-Port 8799)) {
        if (Test-Path $galatea) {
            Write-Host "  waking Galatea in her own window..." -ForegroundColor DarkCyan
            Start-Process powershell -ArgumentList @(
                "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $galatea
            ) -WorkingDirectory (Split-Path -Parent $galatea)
        } else {
            Write-Host "  ! Galatea's launcher was not found at $galatea; skipping the voice." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  Galatea is already awake on 8799." -ForegroundColor DarkCyan
    }
}

Write-Host ""
Write-Host "  PIONIR" -ForegroundColor Cyan
Write-Host "  the brain is awake while this window is open; close it or Ctrl+C to stop." -ForegroundColor DarkCyan
Write-Host ""

$argv = @("-m", "pionir", "server", "--port", "$Port")
if ($NoBrowser) { $argv += "--no-browser" }
& $py @argv
exit $LASTEXITCODE
