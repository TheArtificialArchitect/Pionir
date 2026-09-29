# VPS lockdown: the trading APIs behind the SSH tunnel, the Robinhood key rotated, a
# read-only key for the dashboards. Ian runs this; it is idempotent and a DRY RUN unless
# -Apply is given. It prints every step and NEVER prints a key (the output carries names,
# paths and HTTP status codes only). Windows PowerShell 5.1. It installs nothing that
# starts on its own: no service, task or Run key here, and on the VPS only an ssh account.
#
#   .\tools\vps-lockdown.ps1                      dry run: what it would do, touches nothing
#   .\tools\vps-lockdown.ps1 -Inspect             read-only look at the VPS (units, env key
#                                                 NAMES, sshd, ufw, listeners), changes nothing
#   .\tools\vps-lockdown.ps1 -Apply               do it (steps a-h below)
#   .\tools\vps-lockdown.ps1 -FinishRotation -Apply
#                                                 after the phone has its new build: the old
#                                                 Robinhood key stops working
#   -ClosePorts 8000,8001,8002 -PhoneMovedOffPublicPorts
#                                                 close the public ports - ONLY once nothing
#                                                 reaches them over the internet any more
#   -SkipPhone                                    do not update the phone's GitHub secrets or
#                                                 start its build (then do it by hand, see [f])
#
# Steps (-Apply):
#   [0] preflight   local tools, the deploy key, the VPS host key in known_hosts, the new
#                   robinhood_read_api.py (trading-bot-app branch read-key must be merged)
#   [a] keys        new random keys (32 bytes, urlsafe, 43 chars), made once and kept in
#                   ~\.pionir\vault\vps-lockdown.json (owner-only) until the rotation ends,
#                   so a re-run after a failure uses the same ones
#   [b] tunnel key  a restricted ed25519 key (~\.pionir\secrets\vps-tunnel-key) for the
#                   unprivileged account pionir-tunnel whose authorized_keys allows ONLY
#                   forwarding to 127.0.0.1:8000-8002: no shell, pty, agent or X11
#   [c] deploy      robinhood_read_api.py (read key + rotation window) over the running
#                   one - backed up first, compile-checked, same owner and mode
#   [d] env         PRO_RH_API_KEY rotated (the old one kept as PRO_RH_API_KEY_PREVIOUS so
#                   the phone keeps its stop button until its new build), PRO_RH_READ_KEY
#                   added, PROM_API_KEY and KARKINOS_API_KEY rotated - each env file backed
#                   up first; the values travel over ssh STDIN, never on a command line
#   [e] restart     systemctl try-restart (a stopped live-money unit is NEVER started), a new
#                   MainPID and the orders switch unchanged verified, then HTTP through a
#                   temporary tunnel on the new restricted key: status codes only
#   [f] local keys  the desktop gets READ keys only; the full-power Robinhood key goes to
#                   ~\.pionir\vault (the phone's GitHub secret is set from it); old files
#                   backed up to ~\.pionir\vault\backup-<stamp>
#   [g] ports       closes a public port only when asked AND its consumers have moved
#   [h] rollback    printed, with this run's exact backup names
param(
    [switch]$Apply,
    [switch]$Inspect,
    [switch]$FinishRotation,
    [switch]$NewRotation,
    [switch]$SkipPhone,
    [int[]]$ClosePorts = @(),
    [switch]$PhoneMovedOffPublicPorts,
    [string]$VpsHost = "174.138.35.184",
    [string]$TunnelUser = "pionir-tunnel",
    [string]$DeployKey = "",
    [string]$StateRoot = "",
    [string]$ServerFile = "C:\src\trading-bot-app\server\robinhood_read_api.py",
    [string]$PhoneRepo = "PreShotCome/trading-bot-app",
    [int]$VerifyPortBase = 18100
)

$ErrorActionPreference = "Stop"
if (-not $StateRoot) { $StateRoot = Join-Path $env:USERPROFILE ".pionir" }
if (-not $DeployKey) { $DeployKey = Join-Path $env:USERPROFILE "proteus_deploy" }
$secretsDir   = Join-Path $StateRoot "secrets"
$vaultDir     = Join-Path $StateRoot "vault"
$stateFile    = Join-Path $vaultDir "vps-lockdown.json"
$tunnelKey    = Join-Path $secretsDir "vps-tunnel-key"
$remoteModule = Join-Path $PSScriptRoot "vps_lockdown\remote.py"
# The one remote command line: the program and its payload come on stdin, never here.
$RemotePy     = "python3 -c 'import sys,base64;exec(base64.b64decode(sys.stdin.readline().lstrip(chr(65279))))'"
$Opens        = @("127.0.0.1:8000", "127.0.0.1:8001", "127.0.0.1:8002")
$script:secrets = @()     # every key value this run holds: scrubbed from anything printed
$script:failures = 0

# What reaches each public port over the internet (Pionir's inventory, 2026-09-28). A port
# is closed only when -ClosePorts names it AND -PhoneMovedOffPublicPorts says these moved.
$PublicConsumers = @(
    [pscustomobject]@{ Port = 8000; Who = "the phone app (trading-bot-app lib/config.dart: reads, REAL-MONEY orders, /reconnect with your Robinhood login)" },
    [pscustomobject]@{ Port = 8001; Who = "the phone app (lib/config.dart: reads, /pause, never-list, /api/approve|deny|scan|review)" },
    [pscustomobject]@{ Port = 8002; Who = "the phone app (lib/config.dart: reads)" }
)

# ---- output -------------------------------------------------------------------------------
function Scrub([string]$text) {
    if (-not $text) { return "" }
    foreach ($s in $script:secrets) { if ($s -and $s.Length -ge 8) { $text = $text.Replace($s, "<key>") } }
    return $text
}
function Step([string]$id, [string]$title) { Write-Host ""; Write-Host ("[{0}] {1}" -f $id, $title) -ForegroundColor Cyan }
function Say([string]$text) { Write-Host ("    " + (Scrub $text)) }
function Would([string]$text) { Write-Host ("    would: " + (Scrub $text)) -ForegroundColor DarkGray }
function Warn([string]$text) { Write-Host ("    ! " + (Scrub $text)) -ForegroundColor Yellow }
function Bad([string]$text) { $script:failures++; Write-Host ("    FAILED: " + (Scrub $text)) -ForegroundColor Red }
function Stop-Here([string]$text) { Write-Host ("    STOPPED: " + (Scrub $text)) -ForegroundColor Red; exit 1 }
function Tail([string]$text) { if (-not $text) { return "" }; $t = $text.Trim(); if ($t.Length -gt 300) { $t = "..." + $t.Substring($t.Length - 300) }; return $t }

# ---- processes: argv lists, stdin for anything secret --------------------------------------
function Find-Exe([string]$name) {
    $c = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    return $null
}
function Join-Argv([string[]]$argv) {
    $parts = foreach ($a in $argv) {
        if ($a.Contains('"')) { throw "an argument containing a double quote is never passed" }
        if ($a -eq "" -or $a -match "\s") { '"' + $a + '"' } else { $a }
    }
    return ($parts -join " ")
}
function Invoke-Native([string]$exe, [string[]]$argv, [string]$stdin, [int]$timeoutSec = 300) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = Join-Argv $argv
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    # .NET writes the console input encoding's preamble into a redirected stdin as soon as it
    # opens it: with a UTF-8 console that is a BOM. Use UTF-8 without one while starting (not
    # possible without a console - the remote side strips a stray BOM, and no secret is ever
    # sent to anything but ssh this way).
    $prevEnc = $null
    try { $prevEnc = [Console]::InputEncoding; [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false } catch { $prevEnc = $null }
    try { $p = [System.Diagnostics.Process]::Start($psi) }
    finally { if ($prevEnc) { try { [Console]::InputEncoding = $prevEnc } catch { } } }
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    # raw bytes on the base stream (never the StreamWriter's own encoding)
    $raw = $p.StandardInput.BaseStream
    if ($stdin) { $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes($stdin); $raw.Write($bytes, 0, $bytes.Length); $raw.Flush() }
    $raw.Close()
    if (-not $p.WaitForExit($timeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        return [pscustomobject]@{ Code = -1; Out = ""; Err = "timed out after $timeoutSec s" }
    }
    $p.WaitForExit()
    return [pscustomobject]@{ Code = $p.ExitCode; Out = $outTask.Result; Err = $errTask.Result }
}
function Invoke-Remote($payload) {
    $program = [Convert]::ToBase64String([IO.File]::ReadAllBytes($remoteModule))
    $json = ConvertTo-Json -InputObject $payload -Depth 8 -Compress
    $argv = @("-i", $DeployKey, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
              "-o", "ConnectTimeout=15", "root@$VpsHost", $RemotePy)
    $r = Invoke-Native $script:sshExe $argv ($program + "`n" + $json + "`n") 600
    if ($r.Code -eq 255) { Stop-Here ("ssh to root@$VpsHost failed: " + (Tail $r.Err)) }
    $last = @($r.Out -split "`r?`n" | Where-Object { $_.Trim() }) | Select-Object -Last 1
    if (-not $last) { Stop-Here ("the VPS answered nothing for step '" + $payload.step + "': " + (Tail $r.Err)) }
    try { $obj = $last | ConvertFrom-Json } catch { Stop-Here ("the VPS answered step '" + $payload.step + "' with something that is not JSON") }
    if ($obj.error) { Stop-Here ("step '" + $payload.step + "' refused on the VPS: " + $obj.error) }
    return $obj
}

# ---- keys and files -----------------------------------------------------------------------
function New-Key {
    $b = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($b)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
function Protect-Dir([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    & icacls.exe $dir /inheritance:r /grant:r ("{0}:(OI)(CI)F" -f $me) | Out-Null
}
function Write-Secret([string]$path, [string]$value) {
    [IO.File]::WriteAllText($path, $value, (New-Object System.Text.UTF8Encoding $false))
    & icacls.exe $path /inheritance:r /grant:r ("{0}:F" -f $me) | Out-Null
}
function Read-Secret([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return "" }
    $raw = [IO.File]::ReadAllText($path)
    return $raw.Trim([char]0xFEFF, " ", "`t", "`r", "`n")
}
function Load-State {
    if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
    return (Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json)
}
function Save-State($state) {
    Protect-Dir $vaultDir
    Write-Secret $stateFile (ConvertTo-Json -InputObject $state -Depth 6)
}
function Test-Port([int]$p) {
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $p); return $true } catch { return $false } finally { $c.Dispose() }
}

# ---- HTTP through the temporary tunnel: status codes only ----------------------------------
function Get-Status([string]$method, [string]$url, [string]$key, [string]$body) {
    $req = [System.Net.HttpWebRequest]::Create($url)
    $req.Method = $method
    $req.Proxy = $null
    $req.Timeout = 15000
    $req.AllowAutoRedirect = $false
    if ($key) { $req.Headers.Add("x-api-key", $key) }
    try {
        if ($method -ne "GET") {
            $bytes = [Text.Encoding]::UTF8.GetBytes([string]$body)
            $req.ContentType = "application/json"
            $req.ContentLength = $bytes.Length
            $s = $req.GetRequestStream()
            try { $s.Write($bytes, 0, $bytes.Length) } finally { $s.Close() }
        }
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return $code
    } catch {
        $e = $_.Exception
        while ($e -and -not ($e -is [System.Net.WebException])) { $e = $e.InnerException }
        if ($e -and $e.Response) { $code = [int]$e.Response.StatusCode; $e.Response.Close(); return $code }
        return 0
    }
}
function Open-VerifyTunnel {
    $argv = @("-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
              "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15", "-i", $tunnelKey)
    for ($i = 0; $i -lt 3; $i++) { $argv += @("-L", ("127.0.0.1:{0}:127.0.0.1:{1}" -f ($VerifyPortBase + $i), (8000 + $i))) }
    $argv += "$TunnelUser@$VpsHost"
    $p = Start-Process -FilePath $script:sshExe -ArgumentList (Join-Argv $argv) -PassThru -WindowStyle Hidden
    for ($t = 0; $t -lt 40; $t++) {
        if ((Test-Port $VerifyPortBase) -and (Test-Port ($VerifyPortBase + 1)) -and (Test-Port ($VerifyPortBase + 2))) { return $p }
        if ($p.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    Stop-Tree $p
    return $null
}
function Stop-Tree($p) {
    # through Invoke-Native: in Windows PowerShell 5.1 a native command's stderr under
    # $ErrorActionPreference = Stop (taskkill: "not found") would end the whole run
    if ($p) { [void](Invoke-Native (Join-Path $env:SystemRoot "System32\taskkill.exe") @("/PID", [string]$p.Id, "/T", "/F") "" 30) }
}
function Close-VerifyTunnel($p) { Stop-Tree $p }
function Check([string]$label, [string]$method, [int]$port, [string]$path, [string]$key, [string]$body, [int]$want) {
    $code = Get-Status $method ("http://127.0.0.1:{0}{1}" -f $port, $path) $key $body
    $line = "{0,-58} -> {1} (want {2})" -f $label, $code, $want
    if ($code -eq $want) { Write-Host ("    ok   " + $line) -ForegroundColor Green }
    else { $script:failures++; Write-Host ("    BAD  " + $line) -ForegroundColor Red }
}

# ================================================================================================
$mode = "DRY RUN (nothing is changed; -Apply to act, -Inspect for a read-only look at the VPS)"
if ($Apply) { $mode = "APPLY" } elseif ($Inspect) { $mode = "INSPECT (read-only)" }
if ($FinishRotation) { $mode = "FINISH ROTATION - " + $mode }
Write-Host ""
Write-Host "  VPS LOCKDOWN  $VpsHost   $mode" -ForegroundColor Cyan
Write-Host "  Keys are never printed: names, paths and HTTP status codes only." -ForegroundColor DarkGray
$live = $Apply -or $Inspect

foreach ($p in $ClosePorts) { if ($p -notin @(8000, 8001, 8002)) { Stop-Here "-ClosePorts takes 8000, 8001 and/or 8002 only" } }

# ---- [0] preflight --------------------------------------------------------------------------
Step "0" "preflight (this machine)"
$script:sshExe = Find-Exe "ssh"
$keygenExe = Find-Exe "ssh-keygen"
if (-not $script:sshExe -or -not $keygenExe) { Stop-Here "OpenSSH (ssh, ssh-keygen) is not on PATH" }
Say "ssh: $($script:sshExe)"
if (-not (Test-Path -LiteralPath $remoteModule)) { Stop-Here "the remote half is missing: $remoteModule" }
if (Test-Path -LiteralPath $DeployKey) { Say "deploy key (root, used only by this script): $DeployKey" }
elseif ($live) { Stop-Here "no deploy key at $DeployKey" } else { Warn "no deploy key at $DeployKey (needed for -Inspect / -Apply)" }
$serverText = ""
if (Test-Path -LiteralPath $ServerFile) { $serverText = [IO.File]::ReadAllText($ServerFile) }
$serverReady = $serverText.Contains("PRO_RH_READ_KEY") -and $serverText.Contains("PRO_RH_API_KEY_PREVIOUS")
if ($serverReady) { Say "server file: $ServerFile (has the read key and the rotation window)" }
elseif ($Apply -and -not $FinishRotation) { Stop-Here "$ServerFile lacks PRO_RH_READ_KEY / PRO_RH_API_KEY_PREVIOUS: merge trading-bot-app branch read-key first (or pass -ServerFile)" }
else { Warn "$ServerFile lacks the read key: merge trading-bot-app branch read-key before -Apply" }
if ($live) {
    $kh = Invoke-Native $keygenExe @("-F", $VpsHost) "" 30
    if ($kh.Code -ne 0 -or -not $kh.Out.Trim()) { Stop-Here "the VPS host key is not in your known_hosts (StrictHostKeyChecking=yes needs it): ssh -i $DeployKey root@$VpsHost once and check the fingerprint" }
    Say "the VPS host key is known (known_hosts)"
} else { Would "check the VPS host key is in known_hosts (ssh-keygen -F $VpsHost)" }

# ---- discover (read-only) -------------------------------------------------------------------
$disc = $null
$rhEnv = "/opt/prometheus/rh_api.env"; $promEnv = "/opt/prometheus/pro.env"; $karkEnv = "/opt/mrcrab/mrcrab.env"
$rhUnit = "pro-robinhood-api.service"; $promUnit = "prometheus-api.service"; $karkUnit = "mrcrab-api.service"
$rhScript = "/opt/prometheus/robinhood_read_api.py"
Step "0" "the VPS as it is (read-only)"
if ($live) {
    $disc = Invoke-Remote @{ step = "discover"; tunnel_user = $TunnelUser }
    foreach ($api in @("robinhood", "prometheus", "karkinos")) {
        $a = $disc.apis.$api
        if (-not $a.unit) { Warn "$api : no unit found serving it"; continue }
        Say ("{0,-10} {1} ({2}); key {3} in {4}" -f $api, $a.unit, $a.active, $a.key_name, $(if ($a.key_file) { $a.key_file } else { "NO env file" }))
    }
    $rh = $disc.apis.robinhood
    if ($rh.unit) { $rhUnit = $rh.unit; if ($rh.key_file) { $rhEnv = $rh.key_file }; if ($rh.script) { $rhScript = $rh.script } }
    if ($disc.apis.prometheus.unit) { $promUnit = $disc.apis.prometheus.unit; $promEnv = $disc.apis.prometheus.key_file }
    else { $promUnit = $null; $promEnv = $null }
    if ($disc.apis.karkinos.unit) { $karkUnit = $disc.apis.karkinos.unit; $karkEnv = $disc.apis.karkinos.key_file }
    else { $karkUnit = $null; $karkEnv = $null }
    Say ("robinhood script: {0} (read key support: {1}; orders armed now: {2})" -f $rhScript, $rh.read_key_support, $rh.orders_armed)
    Say ("tunnel account {0}: {1}; sshd: {2}" -f $TunnelUser, $(if ($disc.tunnel_user.exists) { "exists" } else { "not yet" }), (($disc.sshd.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ", "))
    Say ("firewall: {0}; tailscale on the VPS: {1}" -f $disc.ufw, $disc.tailscale)
    Say ("listening on 80/443/8000-8002: {0}" -f ((@($disc.listeners) -join "; ")))
    if (@($disc.nginx_to_apis).Count) { Warn ("nginx forwards to an API port: {0} - closing 8000-8002 does not close that" -f (@($disc.nginx_to_apis) -join ", ")) }
    if (-not $rh.unit -or -not $rh.key_file) { Stop-Here "the Robinhood API unit or its env file with PRO_RH_API_KEY was not found" }
} else { Would "read the units serving :8000/:8001/:8002, their env files (key NAMES only), sshd, ufw, listeners, nginx" }
if ($Inspect -and -not $Apply) { Write-Host ""; Write-Host "  inspect done: nothing was changed." -ForegroundColor DarkCyan; exit 0 }

$state = Load-State
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")

# ================================================================================================
if ($FinishRotation) {
    Step "finish" "end the rotation window: the previous Robinhood key stops working"
    if (-not $state -or -not $state.keys) { Stop-Here "no rotation in progress (nothing in $stateFile)" }
    $script:secrets = @($state.keys.rh_full, $state.keys.rh_read, $state.keys.prom, $state.keys.kark)
    $oldFull = Read-Secret (Join-Path $vaultDir ("backup-{0}\proteus-api-key.txt" -f $state.stamp))
    if ($oldFull) { $script:secrets += $oldFull }
    if (-not $Apply) {
        Would "remove PRO_RH_API_KEY_PREVIOUS from $rhEnv (backed up first), try-restart $rhUnit"
        Would "verify through a temporary tunnel: the old full key -> 401, the new one -> 200"
        exit 0
    }
    $env1 = Invoke-Remote @{ step = "env"; stamp = $stamp; files = @(@{ path = $rhEnv; unset = @("PRO_RH_API_KEY_PREVIOUS") }) }
    foreach ($f in $env1.files) { Say ("{0}: removed {1}; backup {2}" -f $f.path, ((@($f.unset)) -join ","), $f.backup) }
    $rs = Invoke-Remote @{ step = "restart"; units = @{ $rhUnit = @("PRO_RH_API_KEY", "PRO_RH_READ_KEY") }; absent = @{ $rhUnit = @("PRO_RH_API_KEY_PREVIOUS") } }
    foreach ($u in $rs.units) { Say ("{0}: {1}, restarted {2}, orders armed {3} -> {4}" -f $u.unit, $u.state, $u.restarted, $u.orders_armed_before, $u.orders_armed_after) }
    $vt = Open-VerifyTunnel
    if (-not $vt) { Bad "the temporary tunnel did not open: check the tunnel key and account ([b])" }
    else {
        try {
            if ($oldFull) { Check "robinhood GET /status with the OLD full key" "GET" $VerifyPortBase "/status" $oldFull "" 401 }
            Check "robinhood GET /status with the new full key" "GET" $VerifyPortBase "/status" $state.keys.rh_full "" 200
            Check "robinhood GET /status with the read key" "GET" $VerifyPortBase "/status" $state.keys.rh_read "" 200
        } finally { Close-VerifyTunnel $vt }
    }
    if ($script:failures) { Stop-Here "$($script:failures) check(s) failed; the state is kept, run it again once fixed" }
    $done = [ordered]@{ stamp = $state.stamp; finished = $stamp; keys = $null }
    Save-State $done
    Write-Host ""; Write-Host "  rotation finished: the old Robinhood key is dead; the keys left the state file (the full one stays in $vaultDir)." -ForegroundColor Green
    exit 0
}

# ---- [a] keys -------------------------------------------------------------------------------
Step "a" "keys"
$rotate = $true
if ($state -and $state.keys) {
    Say ("using the keys made on {0} (a rotation in progress; nothing new is generated)" -f $state.stamp)
    $stamp = $state.stamp
} elseif ($state -and $state.finished -and -not $NewRotation) {
    Say ("the last rotation finished on {0}; keys are not rotated again (pass -NewRotation to rotate)" -f $state.finished)
    $rotate = $false
} else {
    if ($Apply) {
        $state = [ordered]@{ stamp = $stamp; keys = [ordered]@{ rh_full = (New-Key); rh_read = (New-Key); prom = (New-Key); kark = (New-Key) }; done = [ordered]@{} }
        Save-State $state
        $state = Load-State
        Say "made 4 new keys (32 random bytes each, 43 urlsafe characters) -> $stateFile (owner-only)"
    } else { Would "make 4 keys (Robinhood full, Robinhood READ, Prometheus, Karkinos): 32 random bytes, urlsafe, 43 chars; keep them in $stateFile" }
}
if ($state -and $state.keys) { $script:secrets = @($state.keys.rh_full, $state.keys.rh_read, $state.keys.prom, $state.keys.kark) }
function Mark([string]$name) {
    $s = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
    if (-not $s.done) { $s | Add-Member -NotePropertyName done -NotePropertyValue ([pscustomobject]@{}) -Force }
    $s.done | Add-Member -NotePropertyName $name -NotePropertyValue $true -Force
    Save-State $s
}
function Done([string]$name) { return [bool]($state -and $state.done -and $state.done.$name) }

# ---- [b] the tunnel key ---------------------------------------------------------------------
Step "b" "the restricted tunnel key and account"
$authLine = "restrict,port-forwarding," + (($Opens | ForEach-Object { 'permitopen="' + $_ + '"' }) -join ",") + ',command="/bin/false" <public key>'
if ($Apply) {
    Protect-Dir $secretsDir
    if (-not (Test-Path -LiteralPath $tunnelKey)) {
        # an empty passphrase: Invoke-Native writes the command line itself, so "" survives
        # (Windows PowerShell 5.1's own native-argument passing would drop it)
        $kg = Invoke-Native $keygenExe @("-q", "-t", "ed25519", "-N", "", "-C", "pionir-tunnel", "-f", $tunnelKey) "" 60
        if ($kg.Code -ne 0 -or -not (Test-Path -LiteralPath $tunnelKey)) { Stop-Here ("ssh-keygen failed: " + (Tail $kg.Err)) }
        & icacls.exe $tunnelKey /inheritance:r /grant:r ("{0}:F" -f $me) | Out-Null
        Say "made $tunnelKey (ed25519, no passphrase: the tunnel runs unattended; it can only forward)"
    } else { Say "tunnel key exists: $tunnelKey" }
    $pub = (Get-Content -Raw -LiteralPath ($tunnelKey + ".pub")).Trim()
    $tu = Invoke-Remote @{ step = "tunnel_user"; user = $TunnelUser; pubkey = $pub; opens = $Opens }
    Say ("account {0} ({1}); {2}: {3}" -f $tu.user, $(if ($tu.created) { "created" } else { "existed" }), $tu.authorized_keys, $authLine)
    if ($tu.forwarding_allowed -eq $false) { Bad "sshd forbids TCP forwarding for $TunnelUser (AllowTcpForwarding/DisableForwarding): the tunnel cannot work until that is allowed" }
    if ($tu.allowusers) { Warn "sshd AllowUsers is set ($($tu.allowusers)): $TunnelUser must be in it" }
} else {
    Would "ssh-keygen -t ed25519 -f $tunnelKey (if missing; owner-only)"
    Would "on the VPS: useradd --system $TunnelUser (shell nologin); /home/$TunnelUser/.ssh/authorized_keys (root-owned, 644) = $authLine"
}

# ---- [c] deploy the server change -------------------------------------------------------------
Step "c" "deploy robinhood_read_api.py (read key + rotation window)"
if ($Apply -and $rotate) {
    $bytes = [IO.File]::ReadAllBytes($ServerFile)
    $sha = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))).Replace("-", "").ToLower()
    $dep = Invoke-Remote @{ step = "deploy"; unit = $rhUnit; name = "robinhood_read_api.py"; stamp = $stamp; sha256 = $sha
                            content_b64 = [Convert]::ToBase64String($bytes); must_contain = @("PRO_RH_READ_KEY", "PRO_RH_API_KEY_PREVIOUS") }
    if ($dep.unchanged) { Say "$($dep.path) is already this version" } else { Say "$($dep.path) replaced; backup $($dep.backup)" }
    Mark "deploy"
} elseif ($Apply) { Say "no rotation: not deployed" }
else { Would "copy $ServerFile over $rhScript on the VPS (backup .bak-pionir-<stamp>, compile check, same owner/mode) - there is no deploy script for :8000; this is the by-hand scp it replaces" }

# ---- [d] the env files ----------------------------------------------------------------------
Step "d" "rotate the keys in the env files (values over ssh stdin)"
$oldKeys = @{ rh = (Read-Secret (Join-Path $secretsDir "proteus-api-key.txt")); prom = (Read-Secret (Join-Path $secretsDir "prometheus-api-key.txt")); kark = (Read-Secret (Join-Path $secretsDir "karkinos-read-key.txt")) }
$backupDir = Join-Path $vaultDir ("backup-{0}" -f $stamp)
if (-not $oldKeys.rh) { $oldKeys.rh = Read-Secret (Join-Path $backupDir "proteus-api-key.txt") }
if (-not $oldKeys.prom) { $oldKeys.prom = Read-Secret (Join-Path $backupDir "prometheus-api-key.txt") }
if (-not $oldKeys.kark) { $oldKeys.kark = Read-Secret (Join-Path $backupDir "karkinos-read-key.txt") }
foreach ($v in $oldKeys.Values) { if ($v) { $script:secrets += $v } }
if ($oldKeys.prom -and $oldKeys.prom -eq $state.keys.prom) { $oldKeys.prom = "" }   # a re-run: already the new one
if ($oldKeys.kark -and $oldKeys.kark -eq $state.keys.kark) { $oldKeys.kark = "" }
$files = @()
if ($rotate) {
    $files += @{ path = $rhEnv; set = @{ PRO_RH_API_KEY = $state.keys.rh_full; PRO_RH_READ_KEY = $state.keys.rh_read }; keep_previous = @{ PRO_RH_API_KEY = "PRO_RH_API_KEY_PREVIOUS" } }
    if ($promEnv) { $files += @{ path = $promEnv; set = @{ PROM_API_KEY = $state.keys.prom } } } else { Warn "Prometheus: no env file with PROM_API_KEY found; not rotated" }
    if ($karkEnv) { $files += @{ path = $karkEnv; set = @{ KARKINOS_API_KEY = $state.keys.kark } } } else { Warn "Karkinos: no env file with KARKINOS_API_KEY found; not rotated" }
}
$changedUnits = @{}
if ($Apply -and $rotate) {
    $envr = Invoke-Remote @{ step = "env"; stamp = $stamp; files = $files }
    foreach ($f in $envr.files) {
        if ($f.error) { Bad ("{0}: {1}" -f $f.path, $f.error); continue }
        Say ("{0}: changed [{1}]; backup {2}" -f $f.path, ((@($f.changed)) -join ", "), $f.backup)
        if (@($f.changed).Count) {
            if ($f.path -eq $rhEnv) { $changedUnits[$rhUnit] = $true }
            if ($f.path -eq $promEnv) { $changedUnits[$promUnit] = $true }
            if ($f.path -eq $karkEnv) { $changedUnits[$karkUnit] = $true }
        }
    }
    if (-not (Done "deployed_restart")) { $changedUnits[$rhUnit] = $true }   # the new server file needs a restart too
    Mark "env"
} elseif (-not $Apply) {
    Would "back up $rhEnv, $promEnv, $karkEnv to <file>.bak-pionir-<stamp> (on the VPS, 600)"
    Would "$rhEnv : PRO_RH_API_KEY = new; PRO_RH_API_KEY_PREVIOUS = the old one (the phone's, until -FinishRotation); PRO_RH_READ_KEY = new"
    Would "$promEnv : PROM_API_KEY = new;  $karkEnv : KARKINOS_API_KEY = new"
}

# ---- [e] restart and verify -------------------------------------------------------------------
Step "e" "restart (try-restart only) and verify through a temporary tunnel"
$rhRunning = $true
if ($Apply -and $rotate) {
    $expect = @{}
    if ($changedUnits[$rhUnit]) { $expect[$rhUnit] = @("PRO_RH_API_KEY", "PRO_RH_READ_KEY", "PRO_RH_API_KEY_PREVIOUS") }
    if ($promUnit -and $changedUnits[$promUnit]) { $expect[$promUnit] = @("PROM_API_KEY") }
    if ($karkUnit -and $changedUnits[$karkUnit]) { $expect[$karkUnit] = @("KARKINOS_API_KEY") }
    $running = @{}
    if ($expect.Count) {
        $rs = Invoke-Remote @{ step = "restart"; units = $expect }
        foreach ($u in $rs.units) {
            $running[$u.unit] = [bool]$u.restarted
            if (-not $u.restarted) {
                if ($u.note) { Warn ("{0}: {1} - {2}" -f $u.unit, $u.state, $u.note) }
                else { Bad ("{0} did not come back after its restart (state {1}): journalctl -u {0}" -f $u.unit, $u.state) }
                continue
            }
            Say ("{0}: restarted ({1}); orders armed {2} -> {3}; keys present: {4}" -f $u.unit, $u.state, $u.orders_armed_before, $u.orders_armed_after, ((@($u.env_present)) -join ", "))
            if (@($u.env_missing).Count) { Bad ("{0} runs without {1}" -f $u.unit, ((@($u.env_missing)) -join ", ")) }
            if ($u.orders_armed_before -ne $u.orders_armed_after) { Bad ("{0}: the orders switch changed across the restart ({1} -> {2})" -f $u.unit, $u.orders_armed_before, $u.orders_armed_after) }
        }
        Mark "deployed_restart"
    } else { Say "nothing changed since the last run: no restart" }
    $rhRunning = -not ($running.ContainsKey($rhUnit) -and -not $running[$rhUnit])
    $promRunning = $promUnit -and -not ($running.ContainsKey($promUnit) -and -not $running[$promUnit])
    $karkRunning = $karkUnit -and -not ($running.ContainsKey($karkUnit) -and -not $running[$karkUnit])
    $vt = Open-VerifyTunnel
    if (-not $vt) { Bad "the temporary tunnel (the new restricted key) did not open - the Pionir tunnel pane will not work either" }
    else {
        try {
            $b = $VerifyPortBase
            Say "through 127.0.0.1:$b-$($b + 2) -> VPS 8000-8002 on the restricted key:"
            if ($rhRunning) {
                Check "robinhood GET /health" "GET" $b "/health" "" "" 200
                Check "robinhood GET /status with the READ key" "GET" $b "/status" $state.keys.rh_read "" 200
                Check "robinhood POST /never with the READ key (must be refused)" "POST" $b "/never" $state.keys.rh_read "{}" 401
                Check "robinhood GET /status with the new FULL key" "GET" $b "/status" $state.keys.rh_full "" 200
                if ($oldKeys.rh -and $oldKeys.rh -ne $state.keys.rh_full) { Check "robinhood GET /status with the previous key (phone, until finish)" "GET" $b "/status" $oldKeys.rh "" 200 }
            } else { Warn "the Robinhood API is stopped (left stopped): not checked" }
            if ($promRunning) {
                Check "prometheus GET /api/health" "GET" ($b + 1) "/api/health" "" "" 200
                Check "prometheus GET /status with the new key" "GET" ($b + 1) "/status" $state.keys.prom "" 200
                if ($oldKeys.prom) { Check "prometheus GET /status with the OLD key (must be refused)" "GET" ($b + 1) "/status" $oldKeys.prom "" 401 }
            }
            if ($karkRunning) {
                Check "karkinos GET /health" "GET" ($b + 2) "/health" "" "" 200
                Check "karkinos GET /status with the new key" "GET" ($b + 2) "/status" $state.keys.kark "" 200
                if ($oldKeys.kark) { Check "karkinos GET /status with the OLD key (must be refused)" "GET" ($b + 2) "/status" $oldKeys.kark "" 401 }
            }
        } finally { Close-VerifyTunnel $vt }
    }
} elseif (-not $Apply) {
    Would "systemctl try-restart $rhUnit $promUnit $karkUnit (a stopped unit stays stopped); verify a new MainPID, PRO_RH_ORDERS_ENABLED unchanged, the key NAMES in the running env"
    Would "open ssh -N on the NEW restricted key: 127.0.0.1:$VerifyPortBase-$($VerifyPortBase + 2) -> VPS 8000-8002, then check (status codes only):"
    Would "  robinhood /health 200; /status READ key 200; POST /never READ key 401; /status new FULL key 200; previous key 200"
    Would "  prometheus /api/health 200, /status new 200, old 401;  karkinos /health 200, /status new 200, old 401"
}

# ---- [f] this machine's keys, and the phone ----------------------------------------------------
Step "f" "this machine's keys (the desktop gets READ keys only) and the phone"
$deskFiles = @("proteus-api-key.txt", "proteus-read-key.txt", "prometheus-api-key.txt", "karkinos-read-key.txt")
if ($Apply -and $rotate) {
    Protect-Dir $vaultDir
    if (-not (Test-Path -LiteralPath $backupDir)) {
        Protect-Dir $backupDir
        foreach ($n in $deskFiles) { $src = Join-Path $secretsDir $n; if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $backupDir $n) } }
        Say "old key files backed up to $backupDir"
    }
    Write-Secret (Join-Path $secretsDir "proteus-read-key.txt") $state.keys.rh_read
    if ($promEnv) { Write-Secret (Join-Path $secretsDir "prometheus-api-key.txt") $state.keys.prom }
    if ($karkEnv) { Write-Secret (Join-Path $secretsDir "karkinos-read-key.txt") $state.keys.kark }
    $full = Join-Path $secretsDir "proteus-api-key.txt"
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Force; Say "removed $full (the full-power key no longer sits where the desktop reads)" }
    Write-Secret (Join-Path $vaultDir "proteus-api-key.txt") $state.keys.rh_full
    Say "desktop (secrets\): proteus-read-key.txt (READ), prometheus-api-key.txt, karkinos-read-key.txt"
    Say "full-power Robinhood key: $vaultDir\proteus-api-key.txt - read only by this script (to set the phone's secret)"
    if ($SkipPhone) { Warn "-SkipPhone: set the GitHub secrets PRO_RH_API_KEY, PROM_API_KEY, KARKINOS_API_KEY of $PhoneRepo yourself, then run its 'Build & Distribute APK' workflow on main" }
    elseif (Done "phone") { Say "the phone's secrets were already set and its build started in this rotation" }
    else {
        $gh = Find-Exe "gh"
        $authOk = $false
        if ($gh) { $authOk = ((Invoke-Native $gh @("auth", "status") "" 60).Code -eq 0) }
        if (-not $authOk) { Bad "gh (GitHub CLI) is missing or not logged in: set the three secrets of $PhoneRepo by hand (values: $vaultDir\proteus-api-key.txt, $secretsDir\prometheus-api-key.txt, $secretsDir\karkinos-read-key.txt), then run the build" }
        else {
            # the values go in a dotenv file in the owner-only vault, read by gh and deleted at
            # once: never on a command line, and never through a console encoding
            $names = @("PRO_RH_API_KEY")
            $lines = @("PRO_RH_API_KEY=" + $state.keys.rh_full)
            if ($promEnv) { $names += "PROM_API_KEY"; $lines += ("PROM_API_KEY=" + $state.keys.prom) }
            if ($karkEnv) { $names += "KARKINOS_API_KEY"; $lines += ("KARKINOS_API_KEY=" + $state.keys.kark) }
            $dotenv = Join-Path $vaultDir "phone-secrets.env"
            Write-Secret $dotenv (($lines -join "`n") + "`n")
            try { $r = Invoke-Native $gh @("secret", "set", "-f", $dotenv, "--repo", $PhoneRepo) "" 180 }
            finally { Remove-Item -LiteralPath $dotenv -Force -ErrorAction SilentlyContinue }
            $okAll = ($r.Code -eq 0)
            if ($okAll) { Say ("GitHub secrets {0} of {1} set" -f ($names -join ", "), $PhoneRepo) } else { Bad ("gh secret set: " + (Tail $r.Err)) }
            if ($okAll) {
                $wr = Invoke-Native $gh @("workflow", "run", "build.yml", "--repo", $PhoneRepo, "--ref", "main") "" 120
                if ($wr.Code -eq 0) { Say "the phone's build started (main distributes to your phone through Firebase App Distribution)"; Mark "phone" }
                else { Bad ("gh workflow run build.yml: " + (Tail $wr.Err)) }
            }
        }
    }
    Mark "local"
} elseif (-not $Apply) {
    Would "back up $secretsDir\{proteus-api-key,prometheus-api-key,karkinos-read-key}.txt to $vaultDir\backup-<stamp>\"
    Would "write proteus-read-key.txt (READ key), prometheus-api-key.txt, karkinos-read-key.txt (owner-only); DELETE secrets\proteus-api-key.txt"
    Would "keep the full-power Robinhood key only in $vaultDir\proteus-api-key.txt"
    Would "gh secret set -f <vault dotenv, deleted after> --repo $PhoneRepo (PRO_RH_API_KEY, PROM_API_KEY, KARKINOS_API_KEY), then gh workflow run build.yml --ref main"
}

# ---- [g] the public ports ---------------------------------------------------------------------
Step "g" "the public ports"
Say "moved to the SSH tunnel by this change: Pionir Desktop (all three). On the VPS itself (loopback, unaffected): daily_digest.py, deploy_running_service.sh, Pionir's proteus.status."
foreach ($c in $PublicConsumers) {
    $asked = $ClosePorts -contains [int]$c.Port
    if ($asked -and $PhoneMovedOffPublicPorts) { Say (":{0} will be CLOSED to the internet (you said its consumers moved: {1})" -f $c.Port, $c.Who) }
    elseif ($asked) { Warn (":{0} kept OPEN: -ClosePorts names it but -PhoneMovedOffPublicPorts was not given - still used by {1}" -f $c.Port, $c.Who) }
    else { Say (":{0} kept open - still used over the internet by {1}" -f $c.Port, $c.Who) }
}
$toClose = @()
if ($PhoneMovedOffPublicPorts) { $toClose = @($ClosePorts | Sort-Object -Unique) }
if ($toClose.Count) {
    if ($Apply) {
        $fw = Invoke-Remote @{ step = "firewall"; ports = $toClose }
        foreach ($r in $fw.rules) { Say ("ufw {0} -> exit {1}" -f $r.rule, $r.exit) }
        if (-not $fw.ok) { Bad "a ufw rule failed" }
    } else { Would ("ufw allow 22/tcp; (tailscale0 allowed if present); ufw deny {0}; enable ufw with 'default allow incoming' if it was off (only these ports close)" -f (($toClose | ForEach-Object { "$_/tcp" }) -join ", ")) }
} else {
    Say "nothing is closed. To take the phone off the open internet: install Tailscale on the VPS (the phone already runs it), point the app's three base URLs at the VPS's 100.x address, rebuild, then run this with -ClosePorts 8000,8001,8002 -PhoneMovedOffPublicPorts (tailscale0 stays allowed)."
}

# ---- [h] rollback -----------------------------------------------------------------------------
Step "h" "rollback (only if something is wrong)"
if (-not $Apply) { $stamp = "<stamp>" }
$bk = ".bak-pionir-$stamp"
$envList = @($rhEnv, $promEnv, $karkEnv) | Where-Object { $_ }
$unitList = (@($rhUnit, $promUnit, $karkUnit) | Where-Object { $_ }) -join " "
Say "1. the VPS, as root (ssh -i $DeployKey root@$VpsHost):"
foreach ($e in $envList) { Say ("     cp -p {0}{1} {0}" -f $e, $bk) }
Say ("     cp -p {0}{1} {0}      (the server file)" -f $rhScript, $bk)
Say ("     systemctl try-restart {0}" -f $unitList)
Say "2. this machine's key files:  Copy-Item $vaultDir\backup-$stamp\* $secretsDir\"
Say ("3. the phone: Get-Content -Raw $vaultDir\backup-$stamp\proteus-api-key.txt | gh secret set PRO_RH_API_KEY --repo {0}  (and PROM_API_KEY, KARKINOS_API_KEY from the same folder), then gh workflow run build.yml --repo {0} --ref main" -f $PhoneRepo)
Say "4. the tunnel account: ssh ... root@$VpsHost `"rm -f /home/$TunnelUser/.ssh/authorized_keys`"   (or: userdel -r $TunnelUser)"
Say "5. reopen ports (only if [g] closed any): ssh ... root@$VpsHost `"ufw delete deny 8000/tcp`" (8001, 8002 likewise), or `"ufw disable`""
Say "6. then delete $stateFile so the next run starts clean."

Write-Host ""
if (-not $Apply) { Write-Host "  dry run: nothing was changed. Read it, then run with -Inspect, then -Apply." -ForegroundColor DarkCyan; exit 0 }
if ($script:failures) { Write-Host ("  {0} step(s) FAILED - read the red lines above. The keys are kept; running -Apply again repeats only what is needed." -f $script:failures) -ForegroundColor Red; exit 1 }
if ($rotate) {
    Write-Host "  done. The desktop reads through the tunnel pane (pionir.ps1, or Pionir Desktop's 'VPS tunnel')." -ForegroundColor Green
    Write-Host "  When the phone has installed its new build: .\tools\vps-lockdown.ps1 -FinishRotation -Apply" -ForegroundColor Green
} else { Write-Host "  done." -ForegroundColor Green }
exit 0
