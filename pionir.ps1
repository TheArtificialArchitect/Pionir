# Pionir launcher. Foreground only: the brain is awake while this window is open.
# Nothing here installs a task, a service, a Run key or a Startup item.
#
#   .\pionir.ps1                 wake the whole stack and open the dashboard
#   .\pionir.ps1 -NoVoice        don't wake Galatea
#   .\pionir.ps1 -NoSpecialists  don't start Daedalus/Melete; reach whoever's already up
#   .\pionir.ps1 -NoBrowser      don't open a browser (the URL is printed)
#   .\pionir.ps1 -Port 8781      a different dashboard port
#   .\pionir.ps1 -Stop           stop the dashboard and the specialists it started
#
# "Whole stack, one launch". Theo is retired, so Daedalus (the coder, :8771) and
# Melete (the tool-executor, :8770) are Pionir's own services now: this starts
# them directly - their standalone FastAPI servers, no Theo backend - each in its
# own window you can see and close, and wakes Galatea (:8799) the same way.
# Everything stops when its window closes; -Stop tears the services down from here.
param(
    [switch]$NoVoice,
    [switch]$NoSpecialists,
    [switch]$NoBrowser,
    [switch]$Stop,
    [int]$Port = 8780
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$env:PYTHONPATH = Join-Path $root "src"

# Where the specialists' code lives. Overridable in case they are relocated out
# of the (retired) Tech-Support tree later.
$daedalusDir = if ($env:PIONIR_DAEDALUS_DIR) { $env:PIONIR_DAEDALUS_DIR } else { "C:\src\Tech-Support\daedalus" }
$meleteDir   = if ($env:PIONIR_MELETE_DIR)   { $env:PIONIR_MELETE_DIR }   else { "C:\src\Tech-Support\melete" }

function Test-Port([int]$p) {
    try { (New-Object Net.Sockets.TcpClient).Connect("127.0.0.1", $p); return $true }
    catch { return $false }
}

function Stop-Port([int]$p, [string]$label) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($c) { Stop-Process -Id $c.OwningProcess -Force; Write-Host "  stopped $label on $p." }
}

function Start-Specialist([string]$label, [int]$p, [string]$dir, [string]$module) {
    if (Test-Port $p) { Write-Host "  $label already up on $p." -ForegroundColor DarkCyan; return }
    if (-not (Test-Path $dir)) { Write-Host "  ! $label not found at $dir; skipping." -ForegroundColor Yellow; return }
    Write-Host "  starting $label on $p (its own window)..." -ForegroundColor DarkCyan
    $cmd = "`$host.UI.RawUI.WindowTitle = '$label :$p'; Set-Location '$dir'; & '$py' -m $module"
    Start-Process powershell -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $cmd) -WorkingDirectory $dir
}

if ($Stop) {
    Stop-Port $Port "dashboard"
    Stop-Port 8771 "Daedalus"
    Stop-Port 8770 "Melete"
    Write-Host "  (Galatea is left running; use galatea.ps1 -Stop to sleep her.)"
    exit 0
}

# Ollama is what every specialist's model runs on; a down daemon is not fatal to
# the dashboard but every routed turn would fail, so say so plainly.
if (-not (Test-Port 11434)) {
    Write-Host "  ! Ollama is not answering on 127.0.0.1:11434 - start it, or routed turns will fail." -ForegroundColor Yellow
}

# The specialists Pionir now owns.
if (-not $NoSpecialists) {
    Start-Specialist "Daedalus" 8771 $daedalusDir "daedalus.server"
    Start-Specialist "Melete"   8770 $meleteDir   "melete.server"
}

# The voice, in her own window.
$galatea = Join-Path (Split-Path -Parent $root) "Galatea\galatea.ps1"
if (-not $NoVoice) {
    $env:PIONIR_GALATEA_URL = "http://127.0.0.1:8799"
    if (-not (Test-Port 8799)) {
        if (Test-Path $galatea) {
            Write-Host "  waking Galatea (no separate tab; she lives in the dashboard)..." -ForegroundColor DarkCyan
            # -NoBrowser: do not pop her own UI tab. Her glass is baked into the
            # Pionir dashboard's Voice view instead, so there is one window, not two.
            Start-Process powershell -ArgumentList @(
                "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $galatea, "-NoBrowser"
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
Write-Host "  (the specialists keep their own windows; .\pionir.ps1 -Stop tears them down.)" -ForegroundColor DarkGray
Write-Host ""

$argv = @("-m", "pionir", "server", "--port", "$Port")
if ($NoBrowser) { $argv += "--no-browser" }
& $py @argv
exit $LASTEXITCODE
