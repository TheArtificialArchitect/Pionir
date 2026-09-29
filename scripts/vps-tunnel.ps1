# The SSH tunnel to the VPS trading APIs - a pane of the Pionir stack (pionir.ps1 and
# Pionir Desktop both run this file). Foreground only: it lives while its pane/process
# lives and installs nothing (no service, task or Run key - rule 3).
#
# The droplet's three read APIs are reached ONLY through it, on this machine's loopback:
#
#   127.0.0.1:18000 -> VPS 127.0.0.1:8000  Proteus Robinhood API (LIVE money)
#   127.0.0.1:18001 -> VPS 127.0.0.1:8001  Prometheus API
#   127.0.0.1:18002 -> VPS 127.0.0.1:8002  Karkinos read API
#
# (18000-18002: free on this machine and clear of every port the estate uses - 8000 here
# is genesis's.) The keys and figures cross the internet inside SSH, never as plain HTTP.
#
# The login is a RESTRICTED key for an unprivileged account (tools\vps-lockdown.ps1 makes
# both): authorized_keys allows nothing but forwarding to those three loopback ports - no
# shell, no pty, no agent/X11 - so a stolen tunnel key opens three read APIs, not root.
# BatchMode (never a prompt), StrictHostKeyChecking=yes (the VPS host key must already be
# in known_hosts - it is, from the deploy scripts), ExitOnForwardFailure (a local port held
# by someone else is a failure, not a half-tunnel).
#
# When ssh drops it is started again with backoff: 2 s doubling to 5 min, back to 2 s once
# a connection has held for 2 minutes. Stopping the pane (or pionir.ps1 -Stop, or closing
# Pionir Desktop) ends it. Known by COMMAND LINE: vps-tunnel.ps1 (pionir.ps1 $tunnelMatch,
# Pionir Desktop plan.ts TUNNEL_MATCH).
param(
    [string]$KeyFile = "",
    [string]$VpsHost = "174.138.35.184",
    [string]$User = "pionir-tunnel",
    [int]$MaxBackoffSeconds = 300,
    # tests only: stop after this many ssh runs (0 = forever), and shrink the waits
    [int]$MaxAttempts = 0,
    [double]$InitialDelaySeconds = 2
)

$ErrorActionPreference = "Stop"
if (-not $KeyFile) { $KeyFile = Join-Path $HOME ".pionir\secrets\vps-tunnel-key" }

# The forwards: local loopback port -> the VPS's loopback port. Checked against Pionir's
# TUNNEL_PORTS and Pionir Desktop's tunnel.ts by their tests.
$forwards = @(
    "127.0.0.1:18000:127.0.0.1:8000",
    "127.0.0.1:18001:127.0.0.1:8001",
    "127.0.0.1:18002:127.0.0.1:8002"
)

if (-not (Test-Path -LiteralPath $KeyFile)) {
    Write-Host "  no tunnel key at $KeyFile - the tunnel is not set up yet." -ForegroundColor Yellow
    Write-Host "  Run tools\vps-lockdown.ps1 (read it, then -Apply) in C:\src\Pionir to make and install it." -ForegroundColor Yellow
    exit 2
}

$sshArgs = @("-N",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=15",
    "-i", $KeyFile)
foreach ($f in $forwards) { $sshArgs += @("-L", $f) }
$sshArgs += "$User@$VpsHost"

$delay = $InitialDelaySeconds
$attempt = 0
while ($true) {
    $attempt++
    $started = Get-Date
    Write-Host ("[{0}] tunnel: connecting to {1} (loopback 18000-18002 -> VPS 8000-8002)" -f $started.ToString("HH:mm:ss"), $VpsHost) -ForegroundColor DarkCyan
    & ssh @sshArgs
    $code = $LASTEXITCODE
    $held = ((Get-Date) - $started).TotalSeconds
    if ($MaxAttempts -gt 0 -and $attempt -ge $MaxAttempts) {
        Write-Host ("  tunnel: ssh exited {0}; attempt limit reached." -f $code)
        exit $code
    }
    # held a while: that was a drop, not a failing start - retry soon
    if ($held -ge 120) { $delay = $InitialDelaySeconds }
    Write-Host ("[{0}] tunnel DOWN: ssh exited {1} after {2:N0} s; the money feeds say 'tunnel down' until it is back. Retrying in {3} s." -f (Get-Date).ToString("HH:mm:ss"), $code, $held, $delay) -ForegroundColor Yellow
    Start-Sleep -Milliseconds ([int]($delay * 1000))
    $delay = [Math]::Min($delay * 2, $MaxBackoffSeconds)
}
