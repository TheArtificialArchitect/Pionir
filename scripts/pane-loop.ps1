# One pionir.ps1 pane's process, restarted when it dies on its own - visibly, with backoff,
# and only so often. Dot-sourced by the pane's own shell (pionir.ps1 Pane-Cmd), after the
# pane has set its title, folder and environment, so the command runs exactly as it always
# did: a child of the pane, killed with it when the window closes.
#
# Rule 3 holds: nothing here starts on its own. The loop lives INSIDE the foreground pane
# Ian launched; closing the window (or Ctrl+C in the pane) ends it with everything else.
#
# When the command exits:
#   - pionir.ps1 -Stop dropped its stop marker  -> the pane closes (a deliberate stop)
#   - exit 0, or the Ctrl+C exit code          -> NOT restarted: it stopped on purpose
#   - an exit code listed in -NoRestartCodes   -> NOT restarted (e.g. "not set up yet")
#   - its port already answers                 -> NOT restarted: something else serves it
#   - anything else (a crash)                  -> restarted after 5 s, doubling to 60 s;
#     back to 5 s once a run has held for 5 minutes. -MaxCrashes crashes within
#     -WindowMinutes minutes and it GIVES UP, says so in red, and waits for Enter.
# Every exit and restart is also a line in ~/.pionir/logs/launcher.log, so an outage can
# be read afterwards (before this a crashed pane left nothing behind once it was closed).
param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Command,
    [int]$Port = 0,
    [string]$StopMarker = "",
    [int[]]$NoRestartCodes = @(),
    [int]$MaxCrashes = 5,
    [double]$WindowMinutes = 10,
    [double]$FirstDelaySeconds = 5,
    [double]$MaxDelaySeconds = 60,
    [double]$HealthySeconds = 300,
    [string]$LogFile = "",
    # tests only: never wait for Enter
    [switch]$NoPause
)

if (-not $StopMarker) { $StopMarker = Join-Path $env:LOCALAPPDATA "Pionir\stopping" }
if (-not $LogFile) {
    $logDir = if ($env:PIONIR_LAUNCHER_LOG_DIR) { $env:PIONIR_LAUNCHER_LOG_DIR } else { Join-Path $HOME ".pionir\logs" }
    $LogFile = Join-Path $logDir "launcher.log"
}
# Ctrl+C ends a Python process with STATUS_CONTROL_C_EXIT (0xC000013A), read either way.
$ctrlC = @(-1073741510, 3221225786)

function Write-PaneLog([string]$text) {
    try {
        New-Item -ItemType Directory -Force (Split-Path $LogFile) | Out-Null
        Add-Content -Path $LogFile -Value ("{0:o} [{1}] pane '{2}': {3}" -f (Get-Date), $PID, $Title, $text) -Encoding UTF8
    } catch {
        Write-Host "  (the launcher log is not writable: $($_.Exception.Message))" -ForegroundColor DarkGray
    }
}
function Test-PanePort([int]$p) {
    if ($p -le 0) { return $false }
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $p); return $true }
    catch { return $false }
    finally { $c.Dispose() }
}
function Wait-Enter([string]$text, [ConsoleColor]$color) {
    Write-Host ""
    Write-Host $text -ForegroundColor $color
    if (-not $NoPause) { [void](Read-Host) }
}

$crashes = New-Object System.Collections.ArrayList
$delay = $FirstDelaySeconds
$runs = 0
while ($true) {
    $runs++
    $started = Get-Date
    $global:LASTEXITCODE = 0
    Invoke-Expression $Command
    $code = $LASTEXITCODE
    $held = ((Get-Date) - $started).TotalSeconds
    if (Test-Path -LiteralPath $StopMarker) {
        Write-PaneLog "exit $code after pionir.ps1 -Stop; pane closed"
        exit 0
    }
    if ($code -eq 0 -or $ctrlC -contains $code) {
        Write-PaneLog "exit $code (stopped on purpose); not restarted"
        Wait-Enter "  $Title stopped (exit $code) on purpose; not restarted. Press Enter to close this pane." DarkCyan
        exit 0
    }
    if ($NoRestartCodes -contains $code) {
        Write-PaneLog "exit $code (a refusal, not a crash); not restarted"
        Wait-Enter "  $Title stopped on its own (exit $code): a refusal, not a crash, so it is not restarted. Read the reason above; press Enter to close this pane." Yellow
        exit 0
    }
    $now = Get-Date
    [void]$crashes.Add($now)
    foreach ($t in @($crashes)) {
        if (($now - $t).TotalMinutes -gt $WindowMinutes) { [void]$crashes.Remove($t) }
    }
    if ($crashes.Count -ge $MaxCrashes) {
        Write-PaneLog "exit $code; $($crashes.Count) crashes in $WindowMinutes min - GAVE UP"
        Write-Host ""
        Write-Host "  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
        Write-Host "  !!! $Title CRASHED $($crashes.Count) TIMES IN $WindowMinutes MINUTES - GAVE UP, IT IS DOWN." -ForegroundColor Red
        Write-Host "  !!! Last exit code $code. Read the errors above; run 'pionir doctor'." -ForegroundColor Red
        Write-Host "  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
        Wait-Enter "  Press Enter to close this pane (pionir.ps1 starts it again)." Red
        exit 1
    }
    if ($held -ge $HealthySeconds) { $delay = $FirstDelaySeconds }
    Write-PaneLog "exit $code after $([int]$held) s; crash $($crashes.Count) of $MaxCrashes in $WindowMinutes min; restarting in $delay s"
    Write-Host ""
    Write-Host ("  {0} stopped on its own (exit {1}) after {2} s. Restarting in {3} s (crash {4} of {5} in {6} min before it gives up). Ctrl+C here stops it." -f $Title, $code, [int]$held, $delay, $crashes.Count, $MaxCrashes, $WindowMinutes) -ForegroundColor Yellow
    Start-Sleep -Milliseconds ([int]($delay * 1000))
    if (Test-Path -LiteralPath $StopMarker) {
        Write-PaneLog "pionir.ps1 -Stop during the restart wait; pane closed"
        exit 0
    }
    if (Test-PanePort $Port) {
        Write-PaneLog "port $Port answers already (something else serves it); not restarted"
        Wait-Enter "  ${Title}: port $Port already answers - something else started it - so this pane does not start a second copy. Press Enter to close this pane." DarkCyan
        exit 0
    }
    $delay = [Math]::Min($delay * 2, $MaxDelaySeconds)
    Write-PaneLog "restart $runs"
}
