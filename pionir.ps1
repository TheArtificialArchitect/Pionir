# Pionir launcher. Foreground only: the brain is awake while its window is open.
# Nothing here installs a task, a service, a Run key or a Startup item.
#
#   .\pionir.ps1                 wake the whole stack in ONE window, a pane per bridge
#   .\pionir.ps1 -Shortcut       put a "Pionir" launcher icon on the Desktop
#   .\pionir.ps1 -NoVoice        don't wake Galatea
#   .\pionir.ps1 -NoSpecialists  don't start Daedalus/Melete; reach whoever's already up
#   .\pionir.ps1 -NoBryo         don't start Bryo, the observer organism
#   .\pionir.ps1 -NoCrew         don't start the crew (workers, division leaders)
#   .\pionir.ps1 -NoPeter        don't start Peter (the trading-signal feed) or his VPS relay
#   .\pionir.ps1 -NoTunnel       don't open the SSH tunnel to the VPS trading APIs
#   .\pionir.ps1 -NoBrowser      don't open the dashboard in a browser
#   .\pionir.ps1 -Port 8781      a different dashboard port
#   .\pionir.ps1 -Stop           stop the whole stack from anywhere
#
# One window, every bridge a pane: with Windows Terminal (wt.exe) the dashboard
# server, Galatea, Daedalus, Melete, the crew, Peter, his relay and Bryo each get a titled pane in a single
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
    [switch]$NoCrew,
    [switch]$NoPeter,
    [switch]$NoTunnel,
    [switch]$NoBrowser,
    [switch]$Stop,
    [int]$Port = 8780
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# The launcher's log: ~/.pionir/logs/launcher.log, one line per thing it printed, size-capped
# (one rotation, launcher.log.1). Before this a failed launch left nothing to read. Write-Host
# is shadowed so every line the launcher says is also kept, without touching each call.
$logDir  = if ($env:PIONIR_LAUNCHER_LOG_DIR) { $env:PIONIR_LAUNCHER_LOG_DIR } else { Join-Path $HOME ".pionir\logs" }
$logFile = Join-Path $logDir "launcher.log"
$logCap  = 256KB
$script:logWarned = $false
function Write-Log([string]$text) {
    if (-not $text.Trim()) { return }
    try {
        New-Item -ItemType Directory -Force $logDir | Out-Null
        if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt $logCap)) { Move-Item $logFile "$logFile.1" -Force }
        Add-Content -Path $logFile -Value ("{0:o} [{1}] {2}" -f (Get-Date), $PID, $text.Trim()) -Encoding UTF8
    } catch {
        if (-not $script:logWarned) {
            $script:logWarned = $true
            Microsoft.PowerShell.Utility\Write-Host "  (the launcher log is not writable: $($_.Exception.Message))" -ForegroundColor DarkGray
        }
    }
}
function Write-Host {
    param([Parameter(Position = 0)][object]$Object = "", [ConsoleColor]$ForegroundColor, [switch]$NoNewline)
    Write-Log ([string]$Object)
    Microsoft.PowerShell.Utility\Write-Host @PSBoundParameters
}
Write-Log ("launch: " + (($PSBoundParameters.GetEnumerator() | ForEach-Object { "-$($_.Key) $($_.Value)" }) -join " "))

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
# Proteus, the trading system: Peter's feed (The-Web) and the Mr-Crab relay that ships
# his signals to Karkinos on the VPS. Neither repo is edited; both run as they always did.
$peterDir    = if ($env:PIONIR_PETER_DIR)    { $env:PIONIR_PETER_DIR }    else { "C:\src\The-Web" }
$mrCrabDir   = if ($env:PIONIR_MRCRAB_DIR)   { $env:PIONIR_MRCRAB_DIR }   else { "C:\src\Mr-Crab" }
$peterLive   = Join-Path $root "scripts\peter-live.ps1"
$relayScript = Join-Path $mrCrabDir "deploy\desktop\peter-vps-relay.ps1"
# The SSH tunnel to the VPS trading APIs (loopback 18000-18002 -> VPS 8000-8002), on a
# restricted key tools\vps-lockdown.ps1 makes. No key yet: no pane, one line saying so.
$tunnelScript = Join-Path $root "scripts\vps-tunnel.ps1"
$tunnelKey    = Join-Path $HOME ".pionir\secrets\vps-tunnel-key"
$srcDir      = Join-Path $root "src"
$wt          = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\wt.exe"

# The dashboard, opened signed in: /?code=<one-time code> (60 s, used once) becomes an
# HttpOnly session cookie and leaves the address bar at once (src/pionir/signin.py).
# The code is asked of the running Pionir with an HMAC of the dashboard's client token
# - never the token itself, which stays out of every URL and command line - and taken
# only with Pionir's proof, so a squatter on the port gets nothing and is opened
# nothing. Without the token file, or no proof, it is the plain, read-only page.
function Hex-Hmac([byte[]]$key, [string]$text) {
    $h = New-Object System.Security.Cryptography.HMACSHA256 (,$key)
    try { $b = $h.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)) } finally { $h.Dispose() }
    return (($b | ForEach-Object { $_.ToString("x2") }) -join "")
}
function Dashboard-Url([int]$p) {
    $dir = Join-Path $HOME ".pionir\secrets"
    if ($env:PIONIR_STATE_ROOT) { $dir = Join-Path $env:PIONIR_STATE_ROOT "secrets" }
    if ($env:PIONIR_CLIENT_TOKEN_DIR) { $dir = $env:PIONIR_CLIENT_TOKEN_DIR }
    $file = Join-Path $dir "pionir-client-dashboard.token"
    $url = "http://127.0.0.1:$p/"
    if (-not (Test-Path $file)) { return $url }
    $raw = Get-Content $file -Raw -ErrorAction SilentlyContinue
    if (-not $raw) { return $url }
    $key = [Text.Encoding]::UTF8.GetBytes($raw.Trim())
    try {
        $rand = New-Object byte[] 16
        $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
        try { $rng.GetBytes($rand) } finally { $rng.Dispose() }
        $nonce = ($rand | ForEach-Object { $_.ToString("x2") }) -join ""
        $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $body = [Text.Encoding]::UTF8.GetBytes('{"nonce":"' + $nonce + '","ts":' + $ts + '}')
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$p/api/signin_code")
        $req.Method = "POST"
        $req.ContentType = "application/json"
        $req.Proxy = $null
        $req.Timeout = 5000
        $req.Headers.Add("X-Pionir-Sign", (Hex-Hmac $key "signin|$nonce|$ts|$p"))
        $req.ContentLength = $body.Length
        $out = $req.GetRequestStream()
        try { $out.Write($body, 0, $body.Length) } finally { $out.Close() }
        $resp = $req.GetResponse()
        try {
            $reader = New-Object IO.StreamReader($resp.GetResponseStream())
            $answer = $reader.ReadToEnd() | ConvertFrom-Json
        } finally { $resp.Close() }
        $code = [string]$answer.code
        if ($code -and ([string]$answer.proof -eq (Hex-Hmac $key "signin-reply|$nonce|$code|$p"))) {
            return $url + "?code=" + [Uri]::EscapeDataString($code)
        }
    } catch { }
    return $url
}

function Test-Port([int]$p) {
    # Dispose the client either way: a connected socket left open holds the
    # port's accept queue slot until the GC gets to it.
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $p); return $true }
    catch { return $false }
    finally { $c.Dispose() }
}

function Stop-Port([int]$p, [string]$label, [string]$id) {
    # Only what the registry says is ours: a foreign program that happens to hold the port is
    # named and left alone (this used to kill whatever listened there).
    Read-Stack
    $state = Get-PortState $id $p
    if ($state.State -eq 'free') { return }
    if ($state.State -eq 'foreign') {
        Write-Host "  $label's port $p is held by $($state.Name) (pid $($state.OwnerPid)), which is not $label; left alone." -ForegroundColor Yellow
        return
    }
    @($script:listen | Where-Object { $_.Port -eq $p } | ForEach-Object { $_.OwnerPid } | Select-Object -Unique) | ForEach-Object {
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
    }
    Write-Host "  stopped $label on $p."
}

function Stop-Bryo {
    # Bryo has no port; he honours a KILL file at his repo root by checkpointing
    # and exiting cleanly (heartbeat.py: governor.kill_requested -> _die). Write
    # it, wait for the organism (not the viewer) to go, then remove it so the
    # next launch isn't blocked.
    $org = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '-m bryo(\s|$)' -and $_.CommandLine -notmatch 'viewer' }
    if (-not $org) { return }
    # He reads KILL once per tick, and a quiet organism ticks as slowly as every
    # 60 s (heartbeat.py clips the interval to 1..60), so wait out a whole tick.
    # A 21 s wait once reported "stopped" while he kept running in the old window.
    $kill = Join-Path $terrariumDir "KILL"
    Set-Content -Path $kill -Value "pionir.ps1 -Stop" -Encoding UTF8
    $gone = $false
    for ($i = 0; $i -lt 130; $i++) {
        Start-Sleep -Milliseconds 700
        if (-not (Get-Process -Id $org.ProcessId -ErrorAction SilentlyContinue)) { $gone = $true; break }
    }
    if ($gone) {
        Remove-Item $kill -ErrorAction SilentlyContinue
        Write-Host "  stopped Bryo (clean checkpoint + exit)."
    } else {
        # A tick can run far longer than the 60 s sleep (his work happens inside it),
        # and KILL is only read between ticks. Leave it in place: he checkpoints and
        # exits when this tick ends, and the next launch waits for that, then starts
        # him fresh in the new window instead of leaving him in the old one.
        Write-Host "  Bryo is finishing a long tick; he will checkpoint and exit when it ends (KILL left in place)." -ForegroundColor Yellow
    }
}

# One pane = one process tree wt kills on close. The command sets the pane's
# title, moves to the bridge's directory and runs it, base64-encoded so no
# quoting or ';' can be mangled by wt's own command-line parser.
function Enc([string]$command) {
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
}
# A pane closes itself when its bridge was stopped on purpose (-Stop drops the stop
# marker first), and stays open showing the error when a bridge dies on its own.
# Before this, panes were -NoExit shells that -Stop had to kill, and Windows Terminal
# keeps a pane whose process ended with an error code - every restart left a whole
# window of dead panes behind (seven of them by one evening).
$stopMarker = Join-Path $env:LOCALAPPDATA "Pionir\stopping"
# A bridge that dies on its own is restarted IN its pane (scripts\pane-loop.ps1): with backoff
# (5 s doubling to 60 s), never after an exit 0 / Ctrl+C / -Stop, never over a port something
# else now answers on, and it gives up loudly after 5 crashes in 10 minutes. Before this a
# crash waited for Ian to notice the pane (2026-10-07 outage review). The loop lives inside
# the pane he launched: closing the window still brings everything down (rule 3).
$paneLoop = Join-Path $root "scripts\pane-loop.ps1"
function Pane-Cmd([string]$title, [string]$dir, [string]$run, [string]$prelude, [int]$port = 0, [string]$noRestart = "") {
    # PIONIR_PANE marks the shell as one of ours, so Close-EmptyPanes can find it.
    $q = { param($s) $s.Replace("'", "''") }
    $codes = if ($noRestart) { " -NoRestartCodes $noRestart" } else { "" }
    $body = "`$env:PIONIR_PANE='1'; `$host.UI.RawUI.WindowTitle='$title'; Set-Location '$dir'; $prelude" +
            "& '$(& $q $paneLoop)' -Title '$(& $q $title)' -Command '$(& $q $run)' -Port $port -StopMarker '$(& $q $stopMarker)'$codes; exit `$LASTEXITCODE"
    return @("powershell", "-ExecutionPolicy", "Bypass", "-EncodedCommand", (Enc $body))
}

function Close-EmptyPanes {
    # An older launcher's pane was a -NoExit shell, so stopping its bridge left the shell (and the
    # window) behind: three restarts in one night left three windows of 17 empty
    # shells. Close only Pionir's own pane shells - the PIONIR_PANE marker in the
    # decoded command, or the older launcher's exact prelude - that no longer run
    # anything. Never the shell running this script, or any of its ancestors.
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $mine = @{}
    $cur = $PID
    while ($cur -and -not $mine.ContainsKey($cur)) {
        $mine[$cur] = $true
        $cur = ($all | Where-Object { $_.ProcessId -eq $cur } | Select-Object -First 1).ParentProcessId
    }
    $closed = 0
    foreach ($p in $all) {
        if ($p.Name -ne 'powershell.exe' -or $mine.ContainsKey($p.ProcessId)) { continue }
        if ($p.CommandLine -notmatch '-EncodedCommand\s+(\S+)') { continue }
        try { $text = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1])) } catch { continue }
        $ours = $text.StartsWith("`$env:PIONIR_PANE='1';") -or $text.StartsWith("`$host.UI.RawUI.WindowTitle='")
        if (-not $ours) { continue }
        $busy = @($all | Where-Object { $_.ParentProcessId -eq $p.ProcessId -and $_.Name -ne 'conhost.exe' })
        if ($busy.Count -gt 0) { continue }
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $closed++
    }
    if ($closed) { Write-Host "  closed $closed empty pane(s) left by an earlier launch." }
}

# Peter and his relay have no port of their own to stop by (8790 could be anyone's), so they
# are known by COMMAND LINE, and stopped only when one of this launcher's panes started them:
# a pane shell (the PIONIR_PANE marker in its decoded command) among the process's parents.
# Peter started by hand (deploy\Peter.cmd) or by Pionir Desktop is never touched from here.
$peterMatch = '-m peter\.cli\s.*\slive(\s|$)'
$relayMatch = 'peter-vps-relay\.ps1'
# The VPS tunnel is known the same way: its wrapper's command line (ssh is its child).
$tunnelMatch = 'vps-tunnel\.ps1'
function Test-PionirPane($p) {
    if (-not $p -or $p.Name -ne 'powershell.exe' -or [string]$p.CommandLine -notmatch '-EncodedCommand\s+(\S+)') { return $false }
    try { $text = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1])) } catch { return $false }
    return $text.StartsWith("`$env:PIONIR_PANE='1';")
}
function Get-Companions([object[]]$all, [string]$match) {
    # Every process whose command line matches, and whether a Pionir pane is among its
    # parents (four levels: pane -> wrapper powershell -> python, with room to spare).
    foreach ($p in $all) {
        if ([string]$p.CommandLine -notmatch $match) { continue }
        $ours = $false
        $cur = $p
        for ($i = 0; $i -lt 4 -and $cur; $i++) {
            $parentId = $cur.ParentProcessId
            $cur = $all | Where-Object { $_.ProcessId -eq $parentId -and $_.ProcessId -ne $p.ProcessId } | Select-Object -First 1
            if (Test-PionirPane $cur) { $ours = $true; break }
        }
        [pscustomobject]@{ Process = $p; Ours = $ours }
    }
}
function Stop-Companion([string]$match, [string]$label) {
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    foreach ($hit in @(Get-Companions $all $match)) {
        $procId = $hit.Process.ProcessId
        if ($hit.Ours) {
            # the matched process and what it started - never anything above it
            & taskkill.exe /PID $procId /T /F 2>&1 | Out-Null
            Write-Host "  stopped $label (pid $procId, started by a Pionir pane)."
        } else {
            Write-Host "  $label (pid $procId) was not started by this launcher; left alone." -ForegroundColor DarkCyan
        }
    }
}

# ---- Who holds a port, and who started the stack ---------------------------------------
# One list of ports (src\pionir\ports.json) is read here, by pionir doctor and by Pionir
# Desktop's tests. A listening port is not the same as the service (HEAD 3.20): a listener whose
# command line does not match the registry is somebody else's program on our port. Processes
# are read with CIM (psutil cannot read a PowerShell-detached command line, HEAD 3.22).
$registryFile = Join-Path $srcDir "pionir\ports.json"
$script:registry = $null
$script:procs = @{}      # pid -> Win32_Process
$script:listen = @()     # one row per listening TCP port: Port, OwnerPid
function Get-PortSpec([string]$id) {
    if (-not $script:registry) { $script:registry = Get-Content -Raw -Encoding UTF8 $registryFile | ConvertFrom-Json }
    return ($script:registry.services | Where-Object { $_.id -eq $id } | Select-Object -First 1)
}
function Read-Stack {
    $script:procs = @{}
    foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) { $script:procs[[int]$p.ProcessId] = $p }
    $script:listen = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject]@{ Port = [int]$_.LocalPort; OwnerPid = [int]$_.OwningProcess } })
}
function Get-Cmd($p) { return ([string]$p.ExecutablePath) + ' ' + ([string]$p.CommandLine) }
function Test-Answers([int]$p, $spec) {
    if ($spec.probe.kind -ne 'http') { return $true }
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$p$($spec.probe.path)")
        $req.Proxy = $null; $req.Timeout = 2500; $req.ReadWriteTimeout = 2500
        $resp = $req.GetResponse(); $resp.Close(); return $true
    } catch [System.Net.WebException] {
        # any status below 500 is an answer (a 401 from a signed endpoint is the service speaking)
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode; $_.Exception.Response.Close(); return ($code -lt 500) }
        return $false
    } catch { return $false }
}
function Get-PortState([string]$id, [int]$p) {
    # free | ours-healthy | ours-unhealthy | foreign (Name = the program holding it)
    $spec = Get-PortSpec $id
    $holders = @($script:listen | Where-Object { $_.Port -eq $p })
    if (-not $holders.Count) { return [pscustomobject]@{ State = 'free'; OwnerPid = 0; Name = '' } }
    foreach ($h in $holders) {
        $proc = $script:procs[[int]$h.OwnerPid]
        if ($proc -and ((Get-Cmd $proc) -match $spec.match)) {
            $state = if (Test-Answers $p $spec) { 'ours-healthy' } else { 'ours-unhealthy' }
            return [pscustomobject]@{ State = $state; OwnerPid = $h.OwnerPid; Name = $proc.Name }
        }
    }
    $who = $script:procs[[int]$holders[0].OwnerPid]
    $name = if ($who -and $who.Name) { [string]$who.Name } else { 'an unidentified program' }
    return [pscustomobject]@{ State = 'foreign'; OwnerPid = $holders[0].OwnerPid; Name = $name }
}
function Test-DesktopProc($p) {
    # Pionir Desktop: the packaged app, or electron.exe run from its folder (not any shell in it)
    return ($p -and ([string]$p.Name -match '^(Pionir Desktop|electron)\.exe$') -and ((Get-Cmd $p) -match 'pionir[ _-]?desktop'))
}
function Get-Owner([int]$procId) {
    # Walk the parents: a Pionir pane shell -> 'pionir.ps1'; Desktop's electron -> 'desktop';
    # neither (a hand-started process, or a parent that has since exited) -> 'hand'. A parent
    # younger than its child is a recycled pid, not a parent.
    $cur = $script:procs[$procId]
    $seen = @{}
    for ($i = 0; $i -lt 8 -and $cur -and -not $seen.ContainsKey([int]$cur.ProcessId); $i++) {
        $seen[[int]$cur.ProcessId] = $true
        if (Test-PionirPane $cur) { return 'pionir.ps1' }
        if (Test-DesktopProc $cur) { return 'desktop' }
        $parent = $script:procs[[int]$cur.ParentProcessId]
        if ($parent -and $parent.CreationDate -and $cur.CreationDate -and ($parent.CreationDate -gt $cur.CreationDate)) { break }
        $cur = $parent
    }
    return 'hand'
}
function Get-OwnerLabel([string]$owner) {
    switch ($owner) { 'pionir.ps1' { 'this launcher (pionir.ps1)' } 'desktop' { 'Pionir Desktop' } default { 'started by hand' } }
}
function Get-StackOwner([int]$dashboardPort) {
    # Who started the stack that is up now: 'desktop' if any core service is Desktop's, else
    # 'pionir.ps1' if any is a pane's, else 'hand' / 'none'. Desktop wins: two launchers over
    # one stack is the problem, and the one that must not be joined is the one with a manager.
    [void](Get-PortSpec 'dashboard')   # loads the registry
    $owners = @()
    foreach ($svc in $script:registry.services) {
        if (@($svc.launchers) -notcontains 'pionir.ps1' -or @($svc.launchers) -notcontains 'desktop') { continue }
        foreach ($p in @($svc.ports)) {
            $port = if ($svc.id -eq 'dashboard') { $dashboardPort } else { [int]$p }
            $s = Get-PortState $svc.id $port
            if ($s.State -like 'ours-*') { $owners += (Get-Owner ([int]$s.OwnerPid)) }
        }
    }
    if ($owners -contains 'desktop') { return 'desktop' }
    if ($owners -contains 'pionir.ps1') { return 'pionir.ps1' }
    if ($owners.Count) { return 'hand' }
    return 'none'
}
function Test-Running($spec) {
    # Is a process matching this service alive, listening or not? (a slow starter is warming, not down)
    foreach ($p in $script:procs.Values) {
        if ([int]$p.ProcessId -ne $PID -and ((Get-Cmd $p) -match $spec.match)) { return $true }
    }
    return $false
}
function Claim-Port([string]$id, [int]$p, [string]$note = '') {
    # The start decision for one service: 'free' (start it), 'up' (ours, leave it) or 'held'
    # (a foreign program holds the port: never "already up", never started over).
    $spec = Get-PortSpec $id
    $s = Get-PortState $id $p
    switch ($s.State) {
        'free' { return 'free' }
        'ours-healthy' {
            Write-Host "  $($spec.label) already up on $p (pid $($s.OwnerPid), $(Get-OwnerLabel (Get-Owner ([int]$s.OwnerPid)))).$note" -ForegroundColor DarkCyan
            return 'up'
        }
        'ours-unhealthy' {
            Write-Host "  ! $($spec.label) is listening on $p (pid $($s.OwnerPid)) but $($spec.probe.path) does not answer; not started over it. Run pionir doctor." -ForegroundColor Yellow
            return 'up'
        }
        default {
            Write-Host "  !!! port $p is held by $($s.Name) (pid $($s.OwnerPid)), which is not $($spec.label). Not starting it, and not calling it up. Free the port or run pionir doctor." -ForegroundColor Red
            $script:refused += "$($spec.label) :$p (held by $($s.Name))"
            return 'held'
        }
    }
}
$script:refused = @()

if ($Stop) {
    New-Item -ItemType Directory -Force (Split-Path $stopMarker) | Out-Null
    Set-Content -Path $stopMarker -Value (Get-Date -Format o) -Encoding UTF8
    Stop-Port $Port "dashboard" "dashboard"
    Stop-Port 8799 "Galatea" "galatea"
    Stop-Port 8771 "Daedalus" "daedalus"
    Stop-Port 8770 "Melete" "melete"
    Stop-Port 8782 "crew" "crew"
    Stop-Companion $peterMatch "Peter"
    Stop-Companion $relayMatch "Peter's VPS relay"
    Stop-Companion $tunnelMatch "the VPS tunnel"
    Stop-Bryo
    Close-EmptyPanes
    Write-Host "  stack stopped. (Closing the Pionir window does the same thing.)"
    exit 0
}

# Ollama runs every specialist's model; a down daemon is not fatal to the
# dashboard but every routed turn would fail, so say so plainly.
if (-not (Test-Port 11434)) {
    Write-Host "  ! Ollama is not answering on 127.0.0.1:11434 - start it, or routed turns will fail." -ForegroundColor Yellow
}
$env:PIONIR_GALATEA_URL = "http://127.0.0.1:8799"

# Read the machine once: every listener and who owns it. Every start decision below comes from
# this snapshot and the registry (a port held by a foreign program is never "already up").
Read-Stack
$stackOwner = Get-StackOwner $Port
switch ($stackOwner) {
    'desktop'    { Write-Host "  the stack that is up was started by Pionir Desktop; this launcher will not start a second copy beside it." -ForegroundColor DarkCyan }
    'pionir.ps1' { Write-Host "  the stack that is up was started by this launcher (pionir.ps1); starting only what is missing." -ForegroundColor DarkCyan }
    'hand'       { Write-Host "  part of the stack is up, started by hand; adopted as it is." -ForegroundColor DarkCyan }
}

# Bridge tokens. Daedalus (:8771, every repo under C:\src, policy "full") and Melete
# (:8770) take a job from ANY local process unless started with a token: Daedalus reads
# DAEDALUS_TOKEN, Melete MELETE_TOKEN, and then each wants "Authorization: Bearer" on every
# job call. Make ~/.pionir/secrets/daedalus-token.txt and melete-token.txt if missing (32
# random bytes, owner-only file; never overwritten), start each bridge with its token, and
# hand Pionir both. If they cannot be made the bridges are NOT started - an open bridge is
# worse than a missing one.
$savedPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $srcDir
$tokenOut = & python -m pionir bridge-tokens
$bridgeTokensOk = ($LASTEXITCODE -eq 0)
$env:PYTHONPATH = $savedPythonPath
if (-not $bridgeTokensOk) {
    Write-Host "  ! could not make the bridge tokens in ~/.pionir/secrets; Daedalus and Melete will not be started." -ForegroundColor Yellow
} else {
    # A bridge already up without its token (started before the token existed) stays open
    # to every local process until it is restarted: never leave that silent.
    $tokenReport = $null
    try { $tokenReport = ($tokenOut | Out-String) | ConvertFrom-Json } catch { }
    if ($null -eq $tokenReport) {
        Write-Host "  ! could not read the bridge-token report; run 'pionir doctor' to check Daedalus and Melete." -ForegroundColor Yellow
    }
    foreach ($open in @($tokenReport.open_bridges)) {
        if ($open) {
            Write-Host "  !!! $($open.bridge) on :$($open.port) is running WITHOUT its token ($($open.why)): any local process can hand it jobs. Run pionir.ps1 -Stop, then pionir.ps1." -ForegroundColor Red
        }
    }
}

# Decide which bridges this launch brings up: skip any already listening (do not
# double-start and collide on the port), and honour the flags.
$browserFlag = ""
if ($NoBrowser) { $browserFlag = " --no-browser" }
# Atani is not a pane - Pionir shells `atani ...` per call, so the subprocess
# inherits this pane's env. Pin ALL THREE of Atani's model slots to the lean 4B
# instruct: default (its internal reasoning/routing), deliberate (the retired
# depth path), AND teacher (verified 2026-09-13: `atani chat` REPLIES with the
# teacher/voice model, not the default - by default theo-local-v17-q4, a 5.2GB
# GPU tenant that would compete with the voice for the card, which is exactly
# what we are avoiding). All three on the 4B means nothing heavy ever loads for
# Atani and Moss keeps the GPU.
# ATANI_PIONIR_URL: Atani's callback into Pionir (it tasks the doers through
# /api/task). Without it a -Port other than 8780 leaves Atani calling a dead port.
# ATANI_VOICE_RENDERER='0': skip Atani's conversational tone pass - the "donate
# the voice to Moss" seam. Moss is the personality Ian talks to; Atani is the
# background reasoner/router, so it answers plainly. Reversible, no state change,
# and the answer/cycle_id contract is untouched (affect audit, 2026-09-14).
$pionirPrelude = "`$env:PYTHONPATH='$srcDir'; `$env:PIONIR_GALATEA_URL='http://127.0.0.1:8799'; `$env:ATANI_PIONIR_URL='http://127.0.0.1:$Port'; `$env:ATANI_MODEL='qwen3:4b-instruct-2507-q4_K_M'; `$env:ATANI_DELIBERATE_MODEL='qwen3:4b-instruct-2507-q4_K_M'; `$env:ATANI_TEACHER_MODEL='qwen3:4b-instruct-2507-q4_K_M'; `$env:ATANI_VOICE_RENDERER='0'; `$env:PIONIR_DAEDALUS_TOKEN=(Get-Content -Raw (Join-Path `$HOME '.pionir\secrets\daedalus-token.txt')).Trim(); `$env:PIONIR_MELETE_TOKEN=(Get-Content -Raw (Join-Path `$HOME '.pionir\secrets\melete-token.txt')).Trim(); "

$panes = @()   # ordered: dashboard, voice, then the doers
$ports = @()   # the ports this launch is responsible for verifying
if ((Claim-Port 'dashboard' $Port) -eq 'free') {
    $panes += ,(Pane-Cmd "Pionir :$Port" $root "python -m pionir server --port $Port$browserFlag" $pionirPrelude $Port)
    $ports += $Port
}

if (-not $NoVoice) {
    $galateaState = Claim-Port 'galatea' 8799
    if ($galateaState -ne 'free') { }
    elseif (Test-Path $galateaDir) {
        # --phone binds 0.0.0.0 so Ian can reach her from his phone over Tailscale;
        # her token gates every non-loopback request, so this fails closed.
        $panes += ,(Pane-Cmd "Galatea :8799" $galateaDir "python -m galatea wake --port 8799 --no-browser --phone" "" 8799)
        $ports += 8799
    } else { Write-Host "  ! Galatea not found at $galateaDir; skipping the voice." -ForegroundColor Yellow }
}

if (-not $NoSpecialists) {
    if (-not $bridgeTokensOk) { }
    elseif ((Claim-Port 'daedalus' 8771 ' pionir doctor says whether it wants a token.') -ne 'free') { }
    elseif (Test-Path $daedalusDir) {
        # Daedalus runs qwen3-coder:30b - a code-specialist MoE (~3B active),
        # a far stronger coder than a GPU 7B. It is NOT 0 VRAM: measured
        # 2026-09-13, Ollama loads it at ~18-19GB with ~10GB on the card, and
        # loading it evicted gemma3:12b. Daedalus's own config reads these env
        # vars, so pinning them here is all it takes (no Tech-Support edit).
        #
        # The headroom knobs (measured/explained in the daedalus package):
        #   NUM_CTX 32768  - +0.83GB system RAM per +16K, VRAM unchanged, ~8% slower prompt eval
        #   MAX_STEPS 32   - a multi-file change ran out at 16
        #   REPAIRS 4      - one gate rejection used to be the whole recovery
        #   TEMPERATURE .35
        #   THINK 1        - only sent to a model that reports "thinking"; qwen3-coder
        #                    does not (think=true is a 400), so it is inert until the
        #                    model changes. /health brain.thinking_active is the truth.
        $daedalusEnv = "`$env:DAEDALUS_MODEL='qwen3-coder:30b'; `$env:DAEDALUS_NUM_CTX='32768'; `$env:DAEDALUS_MAX_STEPS='32'; `$env:DAEDALUS_REPAIRS='4'; `$env:DAEDALUS_TEMPERATURE='0.35'; `$env:DAEDALUS_THINK='1'; `$env:DAEDALUS_TOKEN=(Get-Content -Raw (Join-Path `$HOME '.pionir\secrets\daedalus-token.txt')).Trim(); if (([string]`$env:DAEDALUS_TOKEN).Length -lt 32) { Write-Host '  no usable DAEDALUS_TOKEN (~/.pionir/secrets/daedalus-token.txt): not starting Daedalus without its token.' -ForegroundColor Red; [void](Read-Host); exit 1 }; "
        $panes += ,(Pane-Cmd "Daedalus :8771" $daedalusDir "python -m daedalus.server" $daedalusEnv 8771)
        $ports += 8771
    } else { Write-Host "  ! Daedalus not found at $daedalusDir; skipping." -ForegroundColor Yellow }
    if (-not $bridgeTokensOk) { }
    elseif ((Claim-Port 'melete' 8770 ' It has a token only if this launcher started it.') -ne 'free') { }
    elseif (Test-Path $meleteDir) {
        $meleteEnv = "`$env:MELETE_TOKEN=(Get-Content -Raw (Join-Path `$HOME '.pionir\secrets\melete-token.txt')).Trim(); if (([string]`$env:MELETE_TOKEN).Length -lt 32) { Write-Host '  no usable MELETE_TOKEN (~/.pionir/secrets/melete-token.txt): not starting Melete without its token.' -ForegroundColor Red; [void](Read-Host); exit 1 }; "
        $panes += ,(Pane-Cmd "Melete :8770" $meleteDir "python -m melete.server" $meleteEnv 8770)
        $ports += 8770
    } else { Write-Host "  ! Melete not found at $meleteDir; skipping." -ForegroundColor Yellow }
}

# The crew: personality-free workers in divisions, leaders that distil, reports up
# to Moss. Its own foreground process (python -m pionir.crew, from this repo) with a
# loopback Direction API on 8782, which Moss reaches only through Pionir's crew.*
# capabilities, so every goal and compute allocation she sets is gated and audited.
# PIONIR_CREW_PIONIR_URL: the crew's hands task Pionir, so a -Port other than 8780
# must reach them too (the same reason Atani gets ATANI_PIONIR_URL above).
if (-not $NoCrew) {
    $crewState = Claim-Port 'crew' 8782
    if ($crewState -eq 'free' -and (Test-Running (Get-PortSpec 'crew'))) {
        Write-Host "  crew is starting (its port opens after the model warm-up); not started twice." -ForegroundColor DarkCyan
    } elseif ($crewState -eq 'free') {
        $crewPrelude = "`$env:PYTHONPATH='$srcDir'; `$env:PIONIR_CREW_PIONIR_URL='http://127.0.0.1:$Port'; `$env:PIONIR_CREW_API_PORT='8782'; "
        $panes += ,(Pane-Cmd "Crew :8782" $root "python -m pionir.crew" $crewPrelude 8782)
        $ports += 8782
    }
}

# Peter, the trading-signal feed (C:\src\The-Web): the app on 8790 AND the collect/journal/P&L
# loop in one process, exactly what deploy\Peter.cmd runs (scripts\peter-live.ps1 loads his
# peter-secrets.ps1 into the pane's environment, never printing it). His distiller calls
# Ollama at a hard-coded 127.0.0.1:11434 and takes no GPU lease; HTTP_PROXY sends those
# calls through Pionir's Ollama gate on 8774 (it runs inside the dashboard server), which
# puts each on the card under the lease - sidelining Moss's idle model only once she has
# stood down - or on the CPU on purpose. NO_PROXY keeps his few plain-HTTP news feeds direct.
# Already running (started by hand)? Adopted as he is: never started twice. His relay ships
# data\signals.json to Karkinos on the VPS every 5 min (Mr-Crab's peter-vps-relay.ps1, as is).
$peterPrelude = "`$env:HTTP_PROXY='http://127.0.0.1:8774'; `$env:NO_PROXY='feeds.bbci.co.uk,feeds.marketwatch.com'; "
$peterStarted = $false
if (-not $NoPeter) {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='powershell.exe'" -ErrorAction SilentlyContinue)
    $peterNow = @($procs | Where-Object { [string]$_.CommandLine -match $peterMatch })
    if ($peterNow.Count) {
        Write-Host "  Peter already running (pid $($peterNow[0].ProcessId)); adopted as he is, not started twice." -ForegroundColor DarkCyan
        Write-Host "    (started outside Pionir, his model calls skip the GPU gate until he is restarted from here.)" -ForegroundColor DarkGray
    } elseif ((Claim-Port 'peter' 8790) -ne 'free') {
    } elseif (Test-Path (Join-Path $peterDir "deploy\peter.ps1")) {
        $panes += ,(Pane-Cmd "Peter :8790" $peterDir "powershell -NoProfile -ExecutionPolicy Bypass -File $peterLive" $peterPrelude 8790)
        $ports += 8790
        $peterStarted = $true
    } else { Write-Host "  ! Peter not found at $peterDir; skipping the feed." -ForegroundColor Yellow }
    if (@($procs | Where-Object { [string]$_.CommandLine -match $relayMatch }).Count) {
        Write-Host "  Peter's VPS relay already running; not started twice." -ForegroundColor DarkCyan
    } elseif (Test-Path $relayScript) {
        $panes += ,(Pane-Cmd "Peter relay (VPS)" $mrCrabDir "powershell -NoProfile -ExecutionPolicy Bypass -File $relayScript" "")
    } else { Write-Host "  ! the relay was not found at $relayScript; Karkinos gets no fresh signals." -ForegroundColor Yellow }
}

# The VPS tunnel: the ONLY way the dashboards reach the droplet's trading APIs (Robinhood
# :8000, Prometheus :8001, Karkinos :8002), as loopback 18000-18002 inside SSH. It restarts
# ssh with backoff when the link drops. One already running (by hand, or Pionir Desktop) is
# left as it is; a loopback port held by another program means no tunnel - never a fall
# back to the public plain-HTTP ports (the feeds then say "tunnel down").
if (-not $NoTunnel) {
    $tprocs = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue)
    $tunnelWrapper = @($tprocs | Where-Object { [string]$_.CommandLine -match $tunnelMatch }).Count
    $tunnelHeld = @(18000, 18001, 18002 | ForEach-Object {
        $s = Get-PortState 'tunnel' $_
        if ($s.State -ne 'free') { [pscustomobject]@{ Port = $_; State = $s.State; Name = $s.Name; OwnerPid = $s.OwnerPid } } })
    $tunnelForeign = @($tunnelHeld | Where-Object { $_.State -eq 'foreign' })
    $tunnelNote = (@($tunnelHeld | ForEach-Object { ":$($_.Port) held by $($_.Name) (pid $($_.OwnerPid))" })) -join '; '
    if ($tunnelForeign.Count) {
        # Loud on purpose: this used to be one yellow line, and the money feeds just said 'tunnel down'.
        $running = if ($tunnelWrapper) { " (a tunnel is running, so the other forwards still work)" } else { " The tunnel is NOT started." }
        Write-Host "  !!! VPS TUNNEL: a program that is not the tunnel holds its loopback port - $tunnelNote.$running The money feeds say 'tunnel down' until it is freed (pionir doctor names it)." -ForegroundColor Red
        $script:refused += "VPS tunnel ($tunnelNote)"
    } elseif ($tunnelWrapper) {
        Write-Host "  VPS tunnel already running; not started twice." -ForegroundColor DarkCyan
    } elseif ($tunnelHeld.Count) {
        Write-Host "  VPS tunnel already open ($tunnelNote); not started twice." -ForegroundColor DarkCyan
    } elseif (-not (Test-Path $tunnelKey)) {
        Write-Host "  VPS tunnel not set up yet (no key at $tunnelKey): run tools\vps-lockdown.ps1 to set it up." -ForegroundColor DarkCyan
    } elseif (Test-Path $tunnelScript) {
        $panes += ,(Pane-Cmd "VPS tunnel" $root "powershell -NoProfile -ExecutionPolicy Bypass -File $tunnelScript" "" 0 "2")
        $ports += 18000
    } else { Write-Host "  ! the tunnel script was not found at $tunnelScript." -ForegroundColor Yellow }
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
    $killFile = Join-Path $terrariumDir "KILL"
    $ourKill = (Test-Path $killFile) -and ((Get-Content $killFile -Raw -ErrorAction SilentlyContinue) -match '^pionir\.ps1 -Stop')
    if ($bryoRunning -and $ourKill) {
        # A -Stop asked him to go and he is finishing a long tick: wait for him (up to
        # 5 min), so he restarts in this window rather than staying in the old one.
        Write-Host "  waiting for Bryo to finish his tick and exit..." -ForegroundColor DarkCyan
        for ($i = 0; $i -lt 300 -and $bryoRunning; $i++) {
            Start-Sleep -Seconds 1
            $bryoRunning = [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match '-m bryo(\s|$)' -and $_.CommandLine -notmatch 'viewer' })
        }
        if ($bryoRunning) {
            # Still busy: withdraw the request so he doesn't exit later with nothing to restart him.
            Remove-Item $killFile -ErrorAction SilentlyContinue
            Write-Host "  ! Bryo is still mid-tick after 5 min; KILL withdrawn, leaving him running where he is." -ForegroundColor Yellow
        }
    }
    if ($bryoRunning) { Write-Host "  Bryo already alive; leaving him be." -ForegroundColor DarkCyan }
    elseif (Test-Path $terrariumDir) {
        # Clear a stale KILL so he doesn't checkpoint-and-exit the moment he boots.
        Remove-Item (Join-Path $terrariumDir "KILL") -ErrorAction SilentlyContinue
        $panes += ,(Pane-Cmd "Bryo (organism)" $terrariumDir "python -m bryo" "")
        $bryoStarted = $true
    } else { Write-Host "  ! terrarium not found at $terrariumDir; no Bryo this run." -ForegroundColor Yellow }
}

function Write-Refused {
    foreach ($r in $script:refused) { Write-Host "  !!! NOT STARTED: $r" -ForegroundColor Red }
}
if ($stackOwner -eq 'desktop' -and $panes.Count -gt 0) {
    # Two launchers over one stack is the port mess: each treats the other's processes as
    # foreign and they fight over the same ports. Desktop's stack is the one running.
    Write-Host ""
    Write-Host "  !!! NOT STARTING: Pionir Desktop already owns this stack, and a second launcher beside it would collide on its ports." -ForegroundColor Red
    Write-Host "      Would have started: $($panes.Count) pane(s) for what is missing. Start those from Pionir Desktop instead," -ForegroundColor Red
    Write-Host "      or close Desktop's stack first (Desktop's own Stop), then run pionir.ps1. Nothing was started or stopped." -ForegroundColor Red
    Write-Refused
    exit 3
}
if ($panes.Count -eq 0) {
    Write-Host "  everything is already up; nothing to start." -ForegroundColor DarkCyan
    Write-Refused
    if (-not $NoBrowser -and (Test-Port $Port)) { Start-Process (Dashboard-Url $Port) }
    exit 0
}

Close-EmptyPanes
# A new launch is not a stop: panes started from here on report a crash instead of
# closing silently. (Cleared only now, after any wait for Bryo above.)
Remove-Item $stopMarker -ErrorAction SilentlyContinue
$usedWt = $false
if (Test-Path $wt) {
    # Assemble one window: first pane is a new-tab; the second splits it into two
    # columns; the rest fill down, alternating columns. Four panes -> a 2x2; five
    # or more keep tiling without any fixed-size table. -w new forces a dedicated
    # window rather than a tab grafted onto an existing one.
    # One named window: every launch lands in the same "pionir" window (a new tab there,
    # the previous tab's panes having closed themselves), never a fresh window each time.
    $wtArgs = @("-w", "pionir", "new-tab") + $panes[0]
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
        # $pane is a full command line ("powershell" ... -EncodedCommand b64);
        # element 0 is the exe, the rest are its arguments.
        Start-Process $pane[0] -ArgumentList $pane[1..($pane.Count - 1)]
    }
}

# Verify the artifact, not the window (HEAD 3.16): a drawn pane is not a live
# server. Wait for each port this launch started to actually answer.
Write-Host "  verifying bridges are actually up..." -ForegroundColor DarkGray
# The crew opens 8782 only after its boot check has warmed and measured its model
# (it can wait on the card), so it may take longer than the others to answer.
$waitSeconds = 30
if ($ports -contains 8782) { $waitSeconds = 90 }
$deadline = (Get-Date).AddSeconds($waitSeconds)
function Get-IdForPort([int]$p) {
    if ($p -eq $Port) { return 'dashboard' }
    return (@($script:registry.services | Where-Object { @($_.ports) -contains $p }) | Select-Object -First 1).id
}
$pending = [System.Collections.ArrayList]@($ports)
while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 800
    @($pending) | ForEach-Object { if (Test-Port $_) { [void]$pending.Remove($_) } }
}
Read-Stack
foreach ($p in $ports) {
    $id = Get-IdForPort $p
    $spec = Get-PortSpec $id
    $s = Get-PortState $id $p
    if ($s.State -eq 'ours-healthy') { Write-Host ("  up   :{0}" -f $p) -ForegroundColor Green }
    elseif ($s.State -eq 'ours-unhealthy') { Write-Host ("  LISTENING :{0} - but {1} does not answer yet" -f $p, $spec.probe.path) -ForegroundColor Yellow }
    elseif ($s.State -eq 'foreign') { Write-Host ("  DOWN :{0} - held by {1} (pid {2}), which is not {3}" -f $p, $s.Name, $s.OwnerPid, $spec.label) -ForegroundColor Red }
    elseif ($spec.slow_start -and (Test-Running $spec)) {
        # alive, port not open yet: the crew warms and measures its model first (it can wait on the card)
        Write-Host ("  WARMING :{0} - {1} is alive; this port opens after its model warm-up. Not down; pionir doctor shows when it is up." -f $p, $spec.label) -ForegroundColor Yellow
    }
    else { Write-Host ("  DOWN :{0} - did not answer in time" -f $p) -ForegroundColor Red }
}
Write-Refused
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
if ($peterStarted -and -not (Test-Port 8774)) {
    Write-Host "  ! Peter's model calls go through Pionir's Ollama gate on 8774, which is not answering: they fail (visibly, in his pane) until the dashboard runs this Pionir (pionir.ps1 -Stop, then pionir.ps1)." -ForegroundColor Yellow
}
if (-not $NoBrowser -and (Test-Port $Port)) { Start-Process (Dashboard-Url $Port) }
Write-Host "  ready." -ForegroundColor DarkCyan
exit 0
