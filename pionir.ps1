# Pionir launcher. Foreground only: the brain is awake while its window is open.
# Nothing here installs a task, a service, a Run key or a Startup item.
#
#   .\pionir.ps1                 wake the whole stack in ONE window, a pane per bridge
#   .\pionir.ps1 -Shortcut       put a "Pionir" launcher icon on the Desktop
#   .\pionir.ps1 -NoVoice        don't wake Galatea
#   .\pionir.ps1 -NoSpecialists  don't start Daedalus/Melete; reach whoever's already up
#   .\pionir.ps1 -NoBryo         don't start Bryo, the observer organism
#   .\pionir.ps1 -NoBrowser      don't open the dashboard in a browser
#   .\pionir.ps1 -Port 8781      a different dashboard port
#   .\pionir.ps1 -Stop           stop the whole stack from anywhere
#
# One window, every bridge a pane: with Windows Terminal (wt.exe) the dashboard
# server, Galatea, Daedalus, Melete and Bryo each get a titled pane in a single
# window. Closing that window brings the whole stack down - wt kills every pane's
# process tree on close (verified: ports free afterwards, no orphans). If wt.exe
# is not present the launcher falls back to one window per bridge. It is a
# foreground launcher Ian runs; it is never a service, autostart or Startup entry
# (rule 3) - Bryo used to run from a logon-triggered task, and this replaces it.
param(
    [switch]$Shortcut,
    [switch]$NoVoice,
    [switch]$NoSpecialists,
    [switch]$NoBryo,
    [switch]$NoBrowser,
    [switch]$Stop,
    [int]$Port = 8780
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if ($Shortcut) {
    # A Desktop icon that runs this launcher. Shortcut only - nothing is added to
    # Startup, no task, no service (rule 3). Hidden so the only window that shows
    # is the one Windows Terminal opens with the panes.
    $desktop = [Environment]::GetFolderPath("Desktop")
    $lnk = Join-Path $desktop "Pionir.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = "powershell.exe"
    $sc.Arguments = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\pionir.ps1`""
    $sc.WorkingDirectory = $root
    $sc.Description = "Pionir - the brain. Awake while its window is open."
    $sc.IconLocation = "%SystemRoot%\System32\shell32.dll,15"
    $sc.Save()
    Write-Host "Desktop icon written: $lnk" -ForegroundColor Cyan
    Write-Host "Double-click it to bring the whole stack up in one window. It starts nothing on its own."
    exit 0
}

# Bridge code lives here. The specialist dirs are overridable in case they move
# out of the (retired) Tech-Support tree later.
$galateaDir  = Join-Path (Split-Path -Parent $root) "Galatea"
$daedalusDir = if ($env:PIONIR_DAEDALUS_DIR) { $env:PIONIR_DAEDALUS_DIR } else { "C:\src\Tech-Support\daedalus" }
$meleteDir   = if ($env:PIONIR_MELETE_DIR)   { $env:PIONIR_MELETE_DIR }   else { "C:\src\Tech-Support\melete" }
$terrariumDir = if ($env:PIONIR_TERRARIUM_DIR) { $env:PIONIR_TERRARIUM_DIR } else { "C:\src\terrarium" }
$srcDir      = Join-Path $root "src"
$wt          = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\wt.exe"

function Test-Port([int]$p) {
    try { (New-Object Net.Sockets.TcpClient).Connect("127.0.0.1", $p); return $true }
    catch { return $false }
}

function Stop-Port([int]$p, [string]$label) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($c) {
        $c | Select-Object -Expand OwningProcess -Unique | ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
        Write-Host "  stopped $label on $p."
    }
}

function Stop-Bryo {
    # Bryo has no port; he honours a KILL file at his repo root by checkpointing
    # and exiting cleanly (heartbeat.py: governor.kill_requested -> _die). Write
    # it, wait for the organism (not the viewer) to go, then remove it so the
    # next launch isn't blocked.
    $org = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '-m bryo(\s|$)' -and $_.CommandLine -notmatch 'viewer' }
    if (-not $org) { return }
    $kill = Join-Path $terrariumDir "KILL"
    Set-Content -Path $kill -Value "pionir.ps1 -Stop" -Encoding UTF8
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 700
        if (-not (Get-Process -Id $org.ProcessId -ErrorAction SilentlyContinue)) { break }
    }
    Remove-Item $kill -ErrorAction SilentlyContinue
    Write-Host "  stopped Bryo (clean checkpoint + exit)."
}

# One pane = one process tree wt kills on close. The command sets the pane's
# title, moves to the bridge's directory and runs it, base64-encoded so no
# quoting or ';' can be mangled by wt's own command-line parser.
function Enc([string]$command) {
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
}
function Pane-Cmd([string]$title, [string]$dir, [string]$run, [string]$prelude) {
    $body = "`$host.UI.RawUI.WindowTitle='$title'; Set-Location '$dir'; $prelude$run"
    return @("powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-EncodedCommand", (Enc $body))
}

if ($Stop) {
    Stop-Port $Port "dashboard"
    Stop-Port 8799 "Galatea"
    Stop-Port 8771 "Daedalus"
    Stop-Port 8770 "Melete"
    Stop-Bryo
    Write-Host "  stack stopped. (Closing the Pionir window does the same thing.)"
    exit 0
}

# Ollama runs every specialist's model; a down daemon is not fatal to the
# dashboard but every routed turn would fail, so say so plainly.
if (-not (Test-Port 11434)) {
    Write-Host "  ! Ollama is not answering on 127.0.0.1:11434 - start it, or routed turns will fail." -ForegroundColor Yellow
}
$env:PIONIR_GALATEA_URL = "http://127.0.0.1:8799"

# Decide which bridges this launch brings up: skip any already listening (do not
# double-start and collide on the port), and honour the flags.
$browserFlag = ""
if ($NoBrowser) { $browserFlag = " --no-browser" }
$pionirPrelude = "`$env:PYTHONPATH='$srcDir'; `$env:PIONIR_GALATEA_URL='http://127.0.0.1:8799'; "

$panes = @()   # ordered: dashboard, voice, then the doers
$ports = @()   # the ports this launch is responsible for verifying
if (-not (Test-Port $Port)) {
    $panes += ,(Pane-Cmd "Pionir :$Port" $root "python -m pionir server --port $Port$browserFlag" $pionirPrelude)
    $ports += $Port
} else { Write-Host "  dashboard already up on $Port." -ForegroundColor DarkCyan }

if (-not $NoVoice) {
    if (Test-Port 8799) { Write-Host "  Galatea already awake on 8799." -ForegroundColor DarkCyan }
    elseif (Test-Path $galateaDir) {
        $panes += ,(Pane-Cmd "Galatea :8799" $galateaDir "python -m galatea wake --port 8799 --no-browser" "")
        $ports += 8799
    } else { Write-Host "  ! Galatea not found at $galateaDir; skipping the voice." -ForegroundColor Yellow }
}

if (-not $NoSpecialists) {
    if (Test-Port 8771) { Write-Host "  Daedalus already up on 8771." -ForegroundColor DarkCyan }
    elseif (Test-Path $daedalusDir) {
        $panes += ,(Pane-Cmd "Daedalus :8771" $daedalusDir "python -m daedalus.server" "")
        $ports += 8771
    } else { Write-Host "  ! Daedalus not found at $daedalusDir; skipping." -ForegroundColor Yellow }
    if (Test-Port 8770) { Write-Host "  Melete already up on 8770." -ForegroundColor DarkCyan }
    elseif (Test-Path $meleteDir) {
        $panes += ,(Pane-Cmd "Melete :8770" $meleteDir "python -m melete.server" "")
        $ports += 8770
    } else { Write-Host "  ! Melete not found at $meleteDir; skipping." -ForegroundColor Yellow }
}

# Bryo, the observer organism. Foreground pane now, not a logon task: he lives
# while the window is open and stops when it closes (crash-safe - he checkpoints
# every tick and replays on restart). He takes a singleton pidfile lock, so skip
# if one is already running. He has no port; he perceives Pionir by reading the
# pulse the dashboard writes, so he only truly observes when the server is up too.
$bryoStarted = $false
if (-not $NoBryo) {
    $bryoRunning = [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '-m bryo(\s|$)' -and $_.CommandLine -notmatch 'viewer' })
    if ($bryoRunning) { Write-Host "  Bryo already alive; leaving him be." -ForegroundColor DarkCyan }
    elseif (Test-Path $terrariumDir) {
        # Clear a stale KILL so he doesn't checkpoint-and-exit the moment he boots.
        Remove-Item (Join-Path $terrariumDir "KILL") -ErrorAction SilentlyContinue
        $panes += ,(Pane-Cmd "Bryo (organism)" $terrariumDir "python -m bryo" "")
        $bryoStarted = $true
    } else { Write-Host "  ! terrarium not found at $terrariumDir; no Bryo this run." -ForegroundColor Yellow }
}

if ($panes.Count -eq 0) {
    Write-Host "  everything is already up; nothing to start." -ForegroundColor DarkCyan
    if (-not $NoBrowser -and (Test-Port $Port)) { Start-Process "http://127.0.0.1:$Port/" }
    exit 0
}

$usedWt = $false
if (Test-Path $wt) {
    # Assemble one window: first pane is a new-tab; the second splits it into two
    # columns; the rest fill down, alternating columns. Four panes -> a 2x2; five
    # or more keep tiling without any fixed-size table. -w new forces a dedicated
    # window rather than a tab grafted onto an existing one.
    $wtArgs = @("-w", "new", "new-tab") + $panes[0]
    for ($i = 1; $i -lt $panes.Count; $i++) {
        if ($i -eq 1) {
            $wtArgs += @(";", "split-pane", "-V") + $panes[$i]      # two columns
        } else {
            $side = if ($i % 2 -eq 0) { "left" } else { "right" }  # fill each column down
            $wtArgs += @(";", "move-focus", $side, ";", "split-pane", "-H") + $panes[$i]
        }
    }
    Start-Process $wt -ArgumentList $wtArgs
    $usedWt = $true
    Write-Host ""
    Write-Host "  PIONIR" -ForegroundColor Cyan
    Write-Host "  one window, a pane per bridge. Close it to bring the whole stack down." -ForegroundColor DarkCyan
} else {
    # Fallback: no Windows Terminal on this machine, so one window per bridge.
    Write-Host "  ! wt.exe not found; falling back to one window per bridge." -ForegroundColor Yellow
    foreach ($pane in $panes) {
        # $pane is a full command line ("powershell" -NoExit ... -EncodedCommand b64);
        # element 0 is the exe, the rest are its arguments.
        Start-Process $pane[0] -ArgumentList $pane[1..($pane.Count - 1)]
    }
}

# Verify the artifact, not the window (HEAD 3.16): a drawn pane is not a live
# server. Wait for each port this launch started to actually answer.
Write-Host "  verifying bridges are actually up..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds(30)
$pending = [System.Collections.ArrayList]@($ports)
while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 800
    @($pending) | ForEach-Object { if (Test-Port $_) { [void]$pending.Remove($_) } }
}
foreach ($p in $ports) {
    if (Test-Port $p) { Write-Host ("  up   :{0}" -f $p) -ForegroundColor Green }
    else { Write-Host ("  DOWN :{0} - did not answer in time" -f $p) -ForegroundColor Red }
}
if ($bryoStarted) {
    # Bryo has no port; verify the organism process actually came up.
    $alive = $false
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 800
        $alive = [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match '-m bryo(\s|$)' -and $_.CommandLine -notmatch 'viewer' })
        if ($alive) { break }
    }
    if ($alive) { Write-Host "  up   :Bryo (organism alive)" -ForegroundColor Green }
    else { Write-Host "  DOWN :Bryo - organism did not come up" -ForegroundColor Red }
}
if (-not $NoBrowser -and (Test-Port $Port)) { Start-Process "http://127.0.0.1:$Port/" }
Write-Host "  ready." -ForegroundColor DarkCyan
exit 0
