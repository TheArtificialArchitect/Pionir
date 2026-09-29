# VPS lockdown: the trading APIs behind the SSH tunnel, the Robinhood key rotated, a
# read-only key for the dashboards. Ian runs this; it is idempotent and a DRY RUN unless
# -Apply is given. It prints every step and NEVER prints a key (the output carries names,
# paths and HTTP status codes only). Windows PowerShell 5.1. It installs nothing that
# starts on its own: no service, task or Run key here, and on the VPS only an ssh account.
#
#   .\tools\vps-lockdown.ps1                      dry run: what it would do, touches nothing
#   .\tools\vps-lockdown.ps1 -Inspect             read-only look at the VPS (units, env key
#                                                 NAMES, sshd, ufw, listeners), changes nothing
#   .\tools\vps-lockdown.ps1 -Apply               steps a-h below: keys, tunnel, tailnet, phone build
#   .\tools\vps-lockdown.ps1 -FinishRotation -Apply
#                                                 once the phone runs its NEW build: you confirm
#                                                 it reads over the tailnet, then the old
#                                                 Robinhood key dies and 8000-8002 close to the
#                                                 internet (tailscale0 stays open), with an
#                                                 automatic revert that only a fresh ssh login
#                                                 cancels; nginx sites forwarding to them go
#   -KeepPublicPorts                              with -FinishRotation: end the rotation only
#   -SkipPhone                                    do not update the phone's GitHub secrets or
#                                                 start its build (then do it by hand, see [p])
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
#   [t] tailnet     Tailscale on the VPS from its official apt repository; `tailscale up
#                   --ssh=false --hostname=proteus-vps --accept-dns=false`; you log in by
#                   clicking the link it prints (no auth key anywhere); the 100.x address and
#                   MagicDNS name go to ~\.pionir\config\vps-tailnet.json (not secret)
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
#   [p] the phone   its GitHub secrets, the repo variable VPS_HOST = the tailnet address, and
#                   its build (main distributes it to the phone)
#   [g] ports       nothing closes in -Apply: that is -FinishRotation, after you confirm
#   [h] rollback    printed, with this run's exact backup names
param(
    [switch]$Apply,
    [switch]$Inspect,
    [switch]$FinishRotation,
    [switch]$NewRotation,
    [switch]$SkipPhone,
    [switch]$KeepPublicPorts,
    [string]$VpsHost = "174.138.35.184",
    [string]$TunnelUser = "pionir-tunnel",
    [string]$DeployKey = "",
    [string]$StateRoot = "",
    # robinhood_read_api.py is deployed from THIS commit of trading-bot-app (the reviewed
    # read-key branch), never from whatever the working tree has checked out, and only if
    # its bytes have exactly this sha256
    [string]$ServerRepo = "C:\src\trading-bot-app",
    [string]$ServerCommit = "b7fea86d51216c71aad893e8755ee3a5d723d96a",
    [string]$ServerSha256 = "19dfc7fa62838e38a5843711c41afa7ad33af298cac31346cbabcc423b07abb3",
    [int]$PreviousDays = 7,
    # the Prometheus auth change is deployed as ONE file too (never the tree deploy, which
    # would ship every undeployed change on pantheon main): pantheon read-key's webapp.py,
    # only onto the reviewed base (main's webapp.py); same rule for the Robinhood file
    [string]$PantheonRepo = "C:\src\pantheon",
    [string]$PromCommit = "dd67d84399dec11e384d08dc2294a855b1327b1e",
    [string]$PromSha256 = "c546a44d68886e614fc488d7f3d85d9ed2b5912ca9e0dd43d8effa102ed5aeab",
    [string]$PromBaseSha256 = "cd372269bef4dba35a1609cd3e30ba61780389b420d0c7d5e58fa2c8c7ae066a",
    # the last commit of pantheon main that was really deployed to the VPS ("Pay off the deploy debt", af9d872):
    # what main gained after it, up to the reviewed base, is listed as not on the VPS
    [string]$PromLastDeployed = "af9d87272fa61628b347015bc3ad1879730d3845",
    [string]$ServerBaseSha256 = "4041b9902c5b1145451a2a6bde9992d2f62b283d35ed3e8a71f2c27431cbb96c",
    [switch]$AllowServerDrift,
    # restarts wait for a quiet moment: outside US market hours and with no trading job
    # running or about to run; -Force overrides (Ian's call)
    [switch]$Force,
    [string]$NowUtc = "",
    [string]$PhoneRepo = "PreShotCome/trading-bot-app",
    [int]$VerifyPortBase = 18100,
    [string]$TailnetHostname = "proteus-vps",
    [string]$TailnetFile = "",
    [int]$LoginWaitMinutes = 15,
    [double]$PollSeconds = 5,
    [int]$RevertAfterSeconds = 300,
    [switch]$SkipTailnetProbe
)

$ErrorActionPreference = "Stop"
if (-not $StateRoot) { $StateRoot = Join-Path $env:USERPROFILE ".pionir" }
if (-not $DeployKey) { $DeployKey = Join-Path $env:USERPROFILE "proteus_deploy" }
$secretsDir   = Join-Path $StateRoot "secrets"
$vaultDir     = Join-Path $StateRoot "vault"
$stateFile    = Join-Path $vaultDir "vps-lockdown.json"
$tunnelKey    = Join-Path $secretsDir "vps-tunnel-key"
if (-not $TailnetFile) { $TailnetFile = Join-Path $StateRoot "config\vps-tailnet.json" }
$remoteModule = Join-Path $PSScriptRoot "vps_lockdown\remote.py"
# The one remote command line: the program and its payload come on stdin, never here.
$RemotePy     = "python3 -c 'import sys,base64;exec(base64.b64decode(sys.stdin.readline().lstrip(chr(65279))))'"
$Opens        = @("127.0.0.1:8000", "127.0.0.1:8001", "127.0.0.1:8002")
$script:secrets = @()     # every key value this run holds: scrubbed from anything printed
$script:failures = 0

# What reaches each public port over the internet (Pionir's inventory, 2026-09-28). They
# close only in -FinishRotation, after you confirm the phone's new build reads over the tailnet.
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

# The same, but an ssh that cannot log in is an answer (null), not the end of the run: used
# to PROVE a fresh login still works around the firewall change.
function Try-Remote($payload) {
    $program = [Convert]::ToBase64String([IO.File]::ReadAllBytes($remoteModule))
    $json = ConvertTo-Json -InputObject $payload -Depth 8 -Compress
    $argv = @("-i", $DeployKey, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
              "-o", "ConnectTimeout=15", "root@$VpsHost", $RemotePy)
    $r = Invoke-Native $script:sshExe $argv ($program + "`n" + $json + "`n") 120
    if ($r.Code -ne 0) { return $null }
    $last = @($r.Out -split "`r?`n" | Where-Object { $_.Trim() }) | Select-Object -Last 1
    try { $obj = $last | ConvertFrom-Json } catch { return $null }
    if ($obj.error) { return $null }
    return $obj
}
function Read-Tailnet {
    if (-not (Test-Path -LiteralPath $TailnetFile)) { return $null }
    try { return (Get-Content -Raw -LiteralPath $TailnetFile | ConvertFrom-Json) } catch { return $null }
}
function Save-Tailnet($obj) {
    $dir = Split-Path -Parent $TailnetFile
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [IO.File]::WriteAllText($TailnetFile, (ConvertTo-Json -InputObject $obj -Depth 4), (New-Object System.Text.UTF8Encoding $false))
}
function Test-RemotePort([string]$hostName, [int]$port, [int]$timeoutMs = 4000) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $ar = $c.BeginConnect($hostName, $port, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne($timeoutMs)) { return $false }
        $c.EndConnect($ar)
        return $true
    } catch { return $false } finally { $c.Close() }
}

function Get-NativeBytes([string]$exe, [string[]]$argv) {
    # stdout as raw bytes (a file's exact content: no console encoding in between)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = Join-Argv $argv
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $ms = New-Object System.IO.MemoryStream
    $p.StandardOutput.BaseStream.CopyTo($ms)
    $p.WaitForExit()
    [void]$errTask.Result
    if ($p.ExitCode -ne 0) { return $null }
    return ,$ms.ToArray()
}
function Get-Sha256Hex([byte[]]$bytes) {
    $h = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($h.ComputeHash($bytes))).Replace("-", "").ToLower() } finally { $h.Dispose() }
}
# the remote half's fingerprint(): compares keys without ever showing one
function Get-NowUtc {
    if ($NowUtc) { return [DateTimeOffset]::Parse($NowUtc).UtcDateTime }   # tests only
    return [DateTime]::UtcNow
}
# US equity market hours: 09:30-16:00 America/New_York, Monday-Friday (holidays not needed)
function Get-MarketWindow([DateTime]$utc) {
    $tz = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $et = [TimeZoneInfo]::ConvertTimeFromUtc($utc, $tz)
    $weekday = $et.DayOfWeek -ne [DayOfWeek]::Saturday -and $et.DayOfWeek -ne [DayOfWeek]::Sunday
    $open = $et.Date.AddHours(9.5); $close = $et.Date.AddHours(16)
    $inHours = $weekday -and $et -ge $open -and $et -lt $close
    $nextSafeEt = $et
    if ($inHours) { $nextSafeEt = $close }
    return [pscustomobject]@{ Open = $inHours; NowEt = $et; NextSafeUtc = [TimeZoneInfo]::ConvertTimeToUtc([DateTime]::SpecifyKind($nextSafeEt, [DateTimeKind]::Unspecified), $tz) }
}
function Assert-SafeToRestart([string]$why) {
    # before any restart of a trading unit: not while the US market is open, not while a job
    # runs or is about to (prometheus-{scan,entry,review,execute}, mrcrab cycles), not while
    # Prometheus has background jobs in flight. -Force overrides; the next safe time is said.
    $w = Get-MarketWindow (Get-NowUtc)
    $req = @{ step = "guard"
        services = @("prometheus-scan.service", "prometheus-entry.service", "prometheus-review.service", "prometheus-execute.service",
                     "mrcrab@t1.service", "mrcrab@research.service", "mrcrab@t2.service", "mrcrab@t3.service")
        timers = @("prometheus-scan.timer", "prometheus-entry.timer", "prometheus-review.timer", "prometheus-execute.timer",
                   "mrcrab-t1.timer", "mrcrab-research.timer", "mrcrab-t2.timer", "mrcrab-t3.timer") }
    if ($promUnit) { $req.jobs_port = 8001; $req.jobs_unit = [string]$promUnit }   # no Prometheus API: no jobs to lose
    $g = Invoke-Remote $req
    $reasons = @()
    if ($w.Open) { $reasons += ("the US market is open (it is {0:HH:mm} in New York; next safe {1:u}, {2:HH:mm} here)" -f $w.NowEt, $w.NextSafeUtc, $w.NextSafeUtc.ToLocalTime()) }
    foreach ($u in @($g.busy)) { if ($u) { $reasons += "$u is running now (wait for it to finish)" } }
    foreach ($d in @($g.due)) { if ($d) { $reasons += ("{0} fires in {1} s (run this after it has)" -f $d.timer, $d.in_s) } }
    if ($g.jobs_running) { $reasons += ("Prometheus has {0} background job(s) in flight (/api/health); wait for them" -f $g.jobs_running) }
    # not knowing is not the same as idle: an unreadable timer or an unanswered health check blocks too
    foreach ($x in @($g.unknown)) { if ($x) { $reasons += ("cannot tell if it is quiet - $x") } }
    if ($null -eq $g.safe) { $reasons += "the guard on the VPS gave no answer" }
    if (-not $reasons.Count) { Say "a quiet moment to restart: market closed, no trading job running or due"; return }
    foreach ($r in $reasons) { Warn $r }
    if ($Force) { Warn "-Force: restarting anyway ($why)"; return }
    Stop-Here "not restarting $why now - nothing was changed. Run it again then (or -Force)."
}
function Show-PromUndeployed([string]$intro) {
    # pantheon main commits after the last real deploy: computed from git (last deployed .. the
    # reviewed base = the pinned commit's parent); a fixed list when the repo cannot say
    $lines = @()
    if ($gitExe -and (Test-Path -LiteralPath $PantheonRepo)) {
        $r = Invoke-Native $gitExe @("-C", $PantheonRepo, "log", "--format=%h %s", "--reverse", ($PromLastDeployed + ".." + $PromCommit + "~1")) "" 60
        if ($r.Code -eq 0) { $lines = @($r.Out -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() }) }
    }
    $from = "git"
    if (-not $lines.Count) {
        $from = "the list embedded in this script (pantheon is not readable here)"
        $lines = @("2a6fe2a Prometheus: observe Peter's derived signals (observational-only)",
                   "0ad778e Prometheus: schedule the equities scan (droplet timer)",
                   "46aa96d Prometheus: backfill open theses for held Robinhood positions (no orders, no arming)",
                   "1e52cd3 Prometheus review: gate the live sell pass behind an explicit arm file")
    }
    Warn ("{0} ({1} commit(s) on pantheon main after its last deploy {2}; from {3}):" -f $intro, $lines.Count, $PromLastDeployed.Substring(0, 7), $from)
    foreach ($l in $lines) { Say ("      " + $l) }
    $touchesWebapp = $false
    if ($from -eq "git") {
        $d = Invoke-Native $gitExe @("-C", $PantheonRepo, "diff", "--name-only", $PromLastDeployed, ($PromCommit + "~1"), "--", "bots/prometheus/src/prometheus/webapp.py") "" 60
        $touchesWebapp = ($d.Code -ne 0) -or [bool]$d.Out.Trim()
    }
    if ($touchesWebapp) { Warn "      some of these change webapp.py itself: the VPS's webapp.py may not be main's (the sha256 base check decides)." }
    else { Say "      This script ships ONE file (webapp.py), which none of these change; it does NOT ship them. Whether the VPS has them is not known from here." }
}
function Get-Fingerprint([string]$value) {
    if (-not $value) { return "" }
    return (Get-Sha256Hex ([Text.Encoding]::UTF8.GetBytes("pionir-lockdown:" + $value))).Substring(0, 16)
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
function Test-RemoteForwardRefused {
    # the restricted key must NOT be able to open a listener on the VPS (-R): sshd's Match
    # block says PermitListen none. ssh with ExitOnForwardFailure exits non-zero at once
    # when refused; one still running after 20 s got its listener.
    $argv = @("-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
              "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15", "-i", $tunnelKey,
              "-R", "127.0.0.1:18999:127.0.0.1:9", "$TunnelUser@$VpsHost")
    $p = Start-Process -FilePath $script:sshExe -ArgumentList (Join-Argv $argv) -PassThru -WindowStyle Hidden
    $null = $p.Handle
    if ($p.WaitForExit(20000)) { return ($p.ExitCode -ne 0) }
    Stop-Tree $p
    return $false
}
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


# ---- [0] preflight --------------------------------------------------------------------------
Step "0" "preflight (this machine)"
$script:sshExe = Find-Exe "ssh"
$keygenExe = Find-Exe "ssh-keygen"
if (-not $script:sshExe -or -not $keygenExe) { Stop-Here "OpenSSH (ssh, ssh-keygen) is not on PATH" }
Say "ssh: $($script:sshExe)"
if (-not (Test-Path -LiteralPath $remoteModule)) { Stop-Here "the remote half is missing: $remoteModule" }
if (Test-Path -LiteralPath $DeployKey) { Say "deploy key (root, used only by this script): $DeployKey" }
elseif ($live) { Stop-Here "no deploy key at $DeployKey" } else { Warn "no deploy key at $DeployKey (needed for -Inspect / -Apply)" }
$serverBytes = $null
$gitExe = Find-Exe "git"
if ($gitExe -and (Test-Path -LiteralPath $ServerRepo)) {
    $serverBytes = Get-NativeBytes $gitExe @("-C", $ServerRepo, "cat-file", "blob", ($ServerCommit + ":server/robinhood_read_api.py"))
}
$serverWhy = ""
if (-not $serverBytes) { $serverWhy = "commit $ServerCommit is not in $ServerRepo (fetch trading-bot-app's read-key branch)" }
elseif ((Get-Sha256Hex $serverBytes) -ne $ServerSha256.ToLower()) { $serverWhy = "server/robinhood_read_api.py at $ServerCommit does not have the pinned sha256" }
else {
    $serverText = [Text.Encoding]::UTF8.GetString($serverBytes)
    if (-not ($serverText.Contains("PRO_RH_READ_KEY") -and $serverText.Contains("PRO_RH_API_KEY_PREVIOUS_UNTIL"))) { $serverWhy = "the pinned file lacks the read key or the previous-key deadline" }
}
$promBytes = $null
if ($gitExe -and (Test-Path -LiteralPath $PantheonRepo)) {
    $promBytes = Get-NativeBytes $gitExe @("-C", $PantheonRepo, "cat-file", "blob", ($PromCommit + ":bots/prometheus/src/prometheus/webapp.py"))
}
$promPinOk = $false
if (-not $promBytes) { Warn "pantheon $($PromCommit.Substring(0, 7)) is not in ${PantheonRepo}: Prometheus's auth change cannot be deployed from here (fetch pantheon's read-key branch)" }
elseif ((Get-Sha256Hex $promBytes) -ne $PromSha256.ToLower()) { Warn "bots/prometheus/src/prometheus/webapp.py at $($PromCommit.Substring(0, 7)) does not have the pinned sha256: not deployed" }
else { $promPinOk = $true; Say ("Prometheus file: pantheon {0} webapp.py (sha256 {1}...), onto main's webapp.py only" -f $PromCommit.Substring(0, 7), $PromSha256.Substring(0, 12)) }
Show-PromUndeployed "not on the VPS unless deployed some other way"
if (-not $serverWhy) { Say ("server file: trading-bot-app {0} server/robinhood_read_api.py (sha256 {1}...)" -f $ServerCommit.Substring(0, 7), $ServerSha256.Substring(0, 12)) }
elseif ($Apply -and -not $FinishRotation) { Stop-Here $serverWhy }
else { Warn $serverWhy }
if ($live) {
    $kh = Invoke-Native $keygenExe @("-F", $VpsHost) "" 30
    if ($kh.Code -ne 0 -or -not $kh.Out.Trim()) { Stop-Here "the VPS host key is not in your known_hosts (StrictHostKeyChecking=yes needs it): ssh -i $DeployKey root@$VpsHost once and check the fingerprint" }
    Say "the VPS host key is known (known_hosts)"
} else { Would "check the VPS host key is in known_hosts (ssh-keygen -F $VpsHost)" }

# ---- discover (read-only) -------------------------------------------------------------------
$disc = $null
$promPrev = $false; $promRead = $false; $karkPrev = $false
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
    if ($rh.active -eq "active" -and $null -ne $rh.orders_armed -and ([bool]$rh.orders_armed -ne [bool]$rh.orders_configured)) {
        Stop-Here ("real-money orders are {0} in the RUNNING Robinhood API but {1} in its configuration: any restart would change that. Nothing was changed. Decide first (Pionir proteus.rh_orders_off / rh_orders_on), then run this again." -f $(if ($rh.orders_armed) { "ARMED" } else { "off" }), $(if ($rh.orders_configured) { "ARMED" } else { "off" }))
    }
    $promPrev = [bool]$disc.apis.prometheus.previous_key_support
    $promRead = [bool]$disc.apis.prometheus.read_key_support
    $karkPrev = [bool]$disc.apis.karkinos.previous_key_support
    Say ("grace window for the phone: Robinhood yes (pinned file); Prometheus {0}; Karkinos {1}. Prometheus read-only key: {2}" -f $(if ($promPrev) { "yes" } else { "NO - its key is not rotated" }), $(if ($karkPrev) { "yes" } else { "NO - its key is not rotated" }), $(if ($promRead) { "yes" } else { "no" }))
} else { Would "read the units serving :8000/:8001/:8002, their env files (key NAMES only), sshd, ufw, listeners, nginx" }
if ($Inspect -and -not $Apply) { Write-Host ""; Write-Host "  inspect done: nothing was changed." -ForegroundColor DarkCyan; exit 0 }

$state = Load-State
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")

# ================================================================================================
function Set-StateField([string]$name, $value) {
    $s = Load-State
    if (-not $s) { $s = [pscustomobject]@{} }
    $s | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
    Save-State $s
}

function Close-PublicPorts($tailnet) {
    # 1. permissions only: 22/tcp FIRST, then 8000-8002 on tailscale0 (nothing denied yet)
    $fp = Invoke-Remote @{ step = "fw_prepare"; stamp = $stamp }
    foreach ($r in $fp.rules) { Say ("ufw {0} -> exit {1}" -f $r.rule, $r.exit) }
    if (-not $fp.ok) { Stop-Here "an allow rule failed: nothing was denied or enabled" }
    Say ("rules backed up to {0} (ufw was {1})" -f $fp.backup, $(if ($fp.was_active) { "active" } else { "off" }))
    # 2. a SECOND, fresh ssh login before anything is denied or enabled
    if (-not (Try-Remote @{ step = "ping" })) { Stop-Here "a fresh ssh login failed after the allow rules: nothing was denied or enabled" }
    Say "a fresh ssh login works"
    # 3. the revert is scheduled first, then the deny and (if it was off) ufw enable
    $arm = Invoke-Remote @{ step = "fw_arm"; stamp = $stamp; revert_after_s = $RevertAfterSeconds }
    foreach ($r in $arm.rules) { Say ("ufw {0} -> exit {1}" -f $r.rule, $r.exit) }
    Say ("the VPS puts the firewall back by itself in {0} s ({1}) unless a fresh login confirms" -f $arm.revert_after_s, $arm.revert_unit)
    # 4. a fresh login proves ssh survived; only then is the revert cancelled
    $again = Try-Remote @{ step = "ping" }
    if (-not $again) {
        Write-Host ("    STOPPED: a fresh ssh login FAILED after the firewall change. Do nothing: the VPS reverts it by itself within {0} s ({1}); then run this again." -f $arm.revert_after_s, $arm.revert_unit) -ForegroundColor Red
        exit 1
    }
    if (-not $arm.ok) {
        Bad ("the rules did not come out as intended (order: tailnet allow {0}, deny {1}, others {2}): the revert is left to run - wait {3} s" -f $arm.order.tailnet_allow, $arm.order.deny, ((@($arm.order.other)) -join ","), $arm.revert_after_s)
        return
    }
    $conf = Invoke-Remote @{ step = "fw_confirm"; revert_unit = $arm.revert_unit }
    if (-not $conf.cancelled) { Bad ("the revert timer is still set: the firewall goes back in {0} s" -f $arm.revert_after_s); return }
    Say "confirmed over a fresh ssh login; the automatic revert is cancelled"
    # 5. seen from here: the public ports shut, the tailnet one open
    foreach ($port in 8000, 8001, 8002) {
        if (Test-RemotePort $VpsHost $port) { Bad ("{0}:{1} still answers from the internet" -f $VpsHost, $port) }
        else { Say ("{0}:{1} no longer answers from the internet" -f $VpsHost, $port) }
    }
    if ($SkipTailnetProbe) { }
    elseif (Test-RemotePort $tailnet.ipv4 8000) { Say ("{0}:8000 answers over the tailnet" -f $tailnet.ipv4) }
    else { Warn ("{0}:8000 did not answer from this machine (is Tailscale running here?) - your phone check is what counts" -f $tailnet.ipv4) }
    if (-not $script:failures) { Set-StateField "ports_closed" $stamp }
}

function Test-KeysShipped {
    # true once this rotation's NEW keys have left the VPS: the phone's GitHub secrets are set ([p]
    # marked "phone") or this machine's key files were written ([f] marked "local"). From then on a
    # rollback of the VPS would kill the very key the phone or the desktop now holds.
    $s = Load-State
    return [bool]($s -and $s.done -and ($s.done.phone -or $s.done.local))
}
function Undo-Rotation([string]$failedUnit, [string]$prefix) {
    # ONE rule for all three APIs, so they never end up on different sides of the rotation:
    #  - nothing has left the VPS yet: every env file goes back to its pre-rotation backup and every
    #    unit that was running is restarted onto it (the failed unit also gets its server file back)
    #  - the new keys HAVE left: FAIL FORWARD. Keep the new keys and the previous ones (still valid until
    #    their deadline; every re-run refreshes a deadline that is less than a day away), change nothing more.
    if (Test-KeysShipped) {
        Warn ("{0}the new keys are already on the phone's build and/or this machine, so they are NOT rolled back (that would lock those out): every API keeps its new keys AND the previous ones." -f $prefix)
        if ($failedUnit) { Warn ("{0} is down or not verified: read journalctl -u {0} on the VPS and fix it (the rollback commands in [h] are for a human decision, not needed for the phone)." -f $failedUnit) }
        Stop-Here "failed forward: nothing was rolled back. Fix what the red lines say, then run -Apply again (same keys; it restarts what is stale, refreshes a previous-key deadline that is near, and verifies)."
    }
    Warn ("{0}not verified: rolling every API back to its pre-rotation env (this rotation's backups)" -f $prefix)
    foreach ($unit in @($unitFiles.Keys)) {
        $files = @($unitFiles[$unit] | Where-Object { $_ })
        if ($unit -ne $failedUnit) { $files = @($files | Where-Object { $_ -like "*.env" }) }
        if (-not $files.Count) { continue }
        $ro = Try-Remote @{ step = "restore"; stamp = $stamp; unit = $unit; files = $files; was_active = [bool]$unitWasActive[$unit] }
        if ($ro) { Say ("{0}: restored ({1}); active {2}" -f $unit, ((@($ro.restored)) -join ", "), $ro.active) }
        else { Warn "$unit : the rollback could not be done - restore by hand (see [h])" }
    }
    if ($failedUnit) { Warn ("{0}: read journalctl -u {0} before running -Apply again" -f $failedUnit) }
    Stop-Here "the rotation is rolled back on the VPS for all three APIs (same keys are used when you run -Apply again). Nothing was written here or sent to the phone."
}

if ($FinishRotation) {
    $tailnet = Read-Tailnet
    $rotationOpen = [bool]($state -and $state.keys)
    if ($rotationOpen) { $script:secrets = @($state.keys.rh_full, $state.keys.rh_read, $state.keys.prom, $state.keys.prom_read, $state.keys.kark) }
    $oldFull = ""
    if ($state -and $state.stamp) { $oldFull = Read-Secret (Join-Path $vaultDir ("backup-{0}\proteus-api-key.txt" -f $state.stamp)) }
    if ($oldFull) { $script:secrets += $oldFull }

    Step "finish-1" "the phone on the tailnet (you confirm)"
    if (-not $tailnet -or -not $tailnet.ipv4) { Stop-Here "the VPS is not on the tailnet yet (no $TailnetFile): run -Apply first" }
    Say ("the new build reads http://{0}:8000-8002 ({1}) - inside WireGuard, never the public IP" -f $tailnet.ipv4, $tailnet.dns_name)
    if (-not $Apply) {
        Would "ask you to open the NEW app build and check a read-only screen (the Karkinos or Pro portfolio) shows numbers; type yes"
        if ($rotationOpen) { Would "remove PRO_RH_API_KEY_PREVIOUS from $rhEnv (backed up), try-restart, verify: old full key 401, new 200" }
        if (-not $KeepPublicPorts) {
            Would "ufw: allow 22/tcp FIRST and 8000-8002 on tailscale0; a fresh ssh login must work before anything is denied"
            Would "schedule an automatic revert ($RevertAfterSeconds s), THEN deny 8000-8002 (enable ufw with 'default allow incoming' if it was off)"
            Would "a fresh ssh login cancels the revert; then check from here: $($VpsHost):8000-8002 closed, the tailnet one open"
            Would "move aside any enabled nginx site that forwards to 8000-8002 (nginx -t first)"
        }
        exit 0
    }
    Write-Host ""
    Write-Host "    Open the Proteus app - the NEW build (installed from Firebase App Distribution) -" -ForegroundColor Magenta
    Write-Host "    and open a READ-ONLY screen: the Karkinos portfolio, or Pro's. Pull to refresh." -ForegroundColor Magenta
    Write-Host "    Does it show your numbers (not an error)? Type yes and press Enter; anything else stops here." -ForegroundColor Magenta
    $answer = [Console]::In.ReadLine()
    if ("$answer".Trim().ToLower() -ne "yes") { Stop-Here "not confirmed: nothing was changed. Check the new build over the tailnet, then run this again." }
    Say "confirmed by you: the phone reads over the tailnet"

    Step "finish-2" "end the rotation window: the previous Robinhood key stops working"
    if ($rotationOpen) {
        Assert-SafeToRestart "the APIs to drop the previous keys"
        $rot = $state.rotated
        if (-not $rot) { $rot = [pscustomobject]@{ rh = $true; prom = $false; kark = $false } }
        $fl = @(@{ path = $rhEnv; unset = @("PRO_RH_API_KEY_PREVIOUS", "PRO_RH_API_KEY_PREVIOUS_UNTIL") })
        if ($rot.prom -and $promEnv) { $fl += @{ path = $promEnv; unset = @("PROM_API_KEY_PREVIOUS", "PROM_API_KEY_PREVIOUS_UNTIL") } }
        if ($rot.kark -and $karkEnv) { $fl += @{ path = $karkEnv; unset = @("KARKINOS_API_KEY_PREVIOUS", "KARKINOS_API_KEY_PREVIOUS_UNTIL") } }
        $env1 = Invoke-Remote @{ step = "env"; stamp = $stamp; files = $fl }
        foreach ($f in $env1.files) { if ($f.error) { Bad ("{0}: {1}" -f $f.path, $f.error) } else { Say ("{0}: removed [{1}]; backup {2}" -f $f.path, ((@($f.unset)) -join ","), $f.backup) } }
        $units = @{}
        $units[$rhUnit] = @{ keys = @("PRO_RH_API_KEY", "PRO_RH_READ_KEY"); absent = @("PRO_RH_API_KEY_PREVIOUS", "PRO_RH_API_KEY_PREVIOUS_UNTIL") }
        if ($rot.prom -and $promUnit) { $units[$promUnit] = @{ keys = @("PROM_API_KEY"); absent = @("PROM_API_KEY_PREVIOUS", "PROM_API_KEY_PREVIOUS_UNTIL") } }
        if ($rot.kark -and $karkUnit) { $units[$karkUnit] = @{ keys = @("KARKINOS_API_KEY"); absent = @("KARKINOS_API_KEY_PREVIOUS", "KARKINOS_API_KEY_PREVIOUS_UNTIL") } }
        $rs = Invoke-Remote @{ step = "restart"; units = $units }
        foreach ($u in $rs.units) {
            if ($u.armed_mismatch) { Bad ("{0}: real-money orders differ between the running process and its configuration - not restarted; decide first" -f $u.unit); continue }
            if (-not $u.was_active) { Warn ("{0}: {1} - {2}" -f $u.unit, $u.state, $u.note); continue }
            if (-not $u.restarted -and -not $u.current) { Bad ("{0} did not come back after its restart (state {1})" -f $u.unit, $u.state); continue }
            Say ("{0}: {1}, orders armed {2} -> {3}" -f $u.unit, $(if ($u.current) { "already current" } else { "restarted" }), $u.orders_armed_before, $u.orders_armed_after)
            if (@($u.env_missing).Count) { Bad ("{0} runs without {1}" -f $u.unit, ((@($u.env_missing)) -join ", ")) }
            if (@($u.env_stale).Count) { Bad ("{0} runs with an old value of {1}" -f $u.unit, ((@($u.env_stale)) -join ", ")) }
            if (@($u.absent_present).Count) { Bad ("{0} still runs with {1}" -f $u.unit, ((@($u.absent_present)) -join ", ")) }
            if ($u.orders_armed_before -ne $u.orders_armed_after) { Bad ("{0}: the orders switch changed across the restart" -f $u.unit) }
        }
        $vt = Open-VerifyTunnel
        if (-not $vt) { Bad "the temporary tunnel did not open: check the tunnel key and account ([b])" }
        else {
            try {
                if ($oldFull) { Check "robinhood GET /status with the OLD full key" "GET" $VerifyPortBase "/status" $oldFull "" 401 }
                Check "robinhood GET /status with the new full key" "GET" $VerifyPortBase "/status" $state.keys.rh_full "" 200
                Check "robinhood GET /status with the read key" "GET" $VerifyPortBase "/status" $state.keys.rh_read "" 200
                $oldProm = Read-Secret (Join-Path $vaultDir ("backup-{0}\prometheus-api-key.txt" -f $state.stamp))
                if ($rot.prom -and $oldProm) { $script:secrets += $oldProm; Check "prometheus GET /status with the OLD key" "GET" ($VerifyPortBase + 1) "/status" $oldProm "" 401 }
                $oldKark = Read-Secret (Join-Path $vaultDir ("backup-{0}\karkinos-read-key.txt" -f $state.stamp))
                if ($rot.kark -and $oldKark) { $script:secrets += $oldKark; Check "karkinos GET /status with the OLD key" "GET" ($VerifyPortBase + 2) "/status" $oldKark "" 401 }
            } finally { Close-VerifyTunnel $vt }
        }
        if ($script:failures) { Stop-Here "$($script:failures) check(s) failed; the state is kept, run it again once fixed (nothing was closed)" }
        Save-State ([ordered]@{ stamp = $state.stamp; finished = $stamp; keys = $null })
        Say "the old Robinhood key is dead; the keys left the state file (the full one stays in $vaultDir)"
    } elseif ($state -and $state.finished) { Say ("already finished on {0}" -f $state.finished) }
    else { Stop-Here "no rotation was ever run (nothing in $stateFile): run -Apply first" }

    Step "finish-3" "close 8000-8002 to the internet (tailscale0 stays open), with an automatic revert"
    $state = Load-State
    if ($KeepPublicPorts) { Warn "-KeepPublicPorts: 8000-8002 stay open to the internet" }
    elseif ($state.ports_closed) { Say ("closed on {0}; checking again from here" -f $state.ports_closed); foreach ($port in 8000, 8001, 8002) { if (Test-RemotePort $VpsHost $port) { Bad ("{0}:{1} answers from the internet again" -f $VpsHost, $port) } } }
    else {
        # After this the tailnet is the phone's ONLY way to the APIs: a node key that expires
        # would cut it off. Refuse while the VPS's (or a phone's) key can expire.
        $ts = Invoke-Remote @{ step = "tailscale_status" }
        $expiring = @()
        if ($ts.key_expiry) { $expiring += ("the VPS (proteus-vps): its key expires {0}" -f $ts.key_expiry) }
        foreach ($m in @($ts.mobiles)) { if ($m -and $m.key_expiry) { $expiring += ("your {0} ({1}): its key expires {2}" -f $m.os, $m.name, $m.key_expiry) } }
        if ($expiring.Count) {
            foreach ($e in $expiring) { Warn $e }
            Say "Turn key expiry off for each: https://login.tailscale.com/admin/machines -> the machine's row (proteus-vps, then your phone) -> the ... menu -> Disable key expiry."
            Say "Then run this again. (Pionir's ssh brakes - proteus.kill / stop_service over :22 - stay an independent STOP route that does not need the tailnet.)"
            Stop-Here "not closing 8000-8002 while a key on the phone's only route can expire"
        }
        Say "Tailscale key expiry is off for the VPS and your phone"
        Close-PublicPorts $tailnet
    }

    Step "finish-4" "nginx sites that forward to the APIs"
    $state = Load-State
    if ($KeepPublicPorts) { Say "left as they are (-KeepPublicPorts)" }
    elseif (-not $state.ports_closed) { Say "not touched: the ports did not close" }
    elseif ($state.nginx_done) { Say ("done on {0}" -f $state.nginx_done) }
    else {
        $ng = Invoke-Remote @{ step = "nginx_disable"; stamp = $stamp }
        if (@($ng.disabled).Count) { foreach ($s in $ng.disabled) { Say ("disabled {0} (moved to {1})" -f $s, $ng.backup) } }
        else { Say "no enabled nginx site forwards to 8000-8002" }
        Set-StateField "nginx_done" $stamp
    }
    Write-Host ""
    if ($script:failures) { Write-Host ("  {0} step(s) FAILED - read the red lines above; run -FinishRotation -Apply again once fixed." -f $script:failures) -ForegroundColor Red; exit 1 }
    Write-Host "  finished: the phone reads over the tailnet, the old key is dead, and 8000-8002 are closed to the internet." -ForegroundColor Green
    Write-Host "  An independent STOP route that needs no tailnet: Pionir's brakes over ssh :22 (proteus.kill, stop_timer, stop_service, rh_orders_off)." -ForegroundColor Green
    Write-Host ("  rollback of the firewall, if ever needed: ssh -i {0} root@{1} `"sh /root/.pionir-ufw-{2}/revert.sh`"" -f $DeployKey, $VpsHost, $state.ports_closed) -ForegroundColor DarkGray
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
        $state = [ordered]@{ stamp = $stamp; keys = [ordered]@{ rh_full = (New-Key); rh_read = (New-Key); prom = (New-Key); prom_read = (New-Key); kark = (New-Key) }; done = [ordered]@{} }
        Save-State $state
        $state = Load-State
        Say ("made 5 new keys (32 random bytes each, 43 urlsafe characters) -> {0} (owner-only)" -f $stateFile)
    } else { Would "make 5 keys (Robinhood full + READ, Prometheus full + READ, Karkinos): 32 random bytes, urlsafe, 43 chars; keep them in $stateFile; the old keys stay valid $PreviousDays days at most (server-enforced)" }
}
if ($state -and $state.keys) { $script:secrets = @($state.keys.rh_full, $state.keys.rh_read, $state.keys.prom, $state.keys.prom_read, $state.keys.kark) }
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

# ---- [t] the tailnet ------------------------------------------------------------------------
Step "t" "the VPS on your tailnet (Tailscale; you log in by clicking a link)"
$tailnet = Read-Tailnet
if ($Apply) {
    $ti = Invoke-Remote @{ step = "tailscale_install" }
    if ($ti.already) { Say "Tailscale is installed ($($ti.version))" } else { Say "installed Tailscale $($ti.version) from its apt repository ($($ti.repo))" }
    $up = Invoke-Remote @{ step = "tailscale_up"; hostname = $TailnetHostname }
    $shown = ""
    $deadline = (Get-Date).AddMinutes($LoginWaitMinutes)
    while ($up.state -ne "Running") {
        if ($up.auth_url -and $up.auth_url -ne $shown) {
            Write-Host ""
            Write-Host ("    >>> open this link and log in to YOUR tailnet: {0}" -f $up.auth_url) -ForegroundColor Magenta
            Write-Host "        (waiting for the login; no auth key is used or stored)" -ForegroundColor DarkGray
            $shown = $up.auth_url
        }
        if ((Get-Date) -gt $deadline) { Stop-Here "no login within $LoginWaitMinutes min: run -Apply again (it picks up where it stopped)" }
        Start-Sleep -Milliseconds ([int]($PollSeconds * 1000))
        $up = Invoke-Remote @{ step = "tailscale_status" }
    }
    if (-not $up.ipv4) { Stop-Here "Tailscale runs on the VPS but reported no 100.x address" }
    $prev = $tailnet
    $tailnet = [ordered]@{ hostname = $up.hostname; ipv4 = $up.ipv4; dns_name = $up.dns_name; key_expiry = $up.key_expiry; recorded = (Get-Date).ToUniversalTime().ToString("o") }
    if ($prev -and $prev.phone_build_host) { $tailnet.phone_build_host = $prev.phone_build_host }
    Save-Tailnet $tailnet
    $tailnet = Read-Tailnet
    Say ("on the tailnet: {0} ({1}) -> {2}" -f $tailnet.ipv4, $tailnet.dns_name, $TailnetFile)
    if ($tailnet.key_expiry) { Warn ("the VPS's Tailscale key expires {0}: disable key expiry for proteus-vps (and your phone) at https://login.tailscale.com/admin/machines before -FinishRotation closes the public ports" -f $tailnet.key_expiry) }
} else {
    Would "install Tailscale from pkgs.tailscale.com's apt repository for the VPS's distro (/etc/os-release) - not curl|sh"
    Would "tailscale up --ssh=false --hostname=$TailnetHostname --accept-dns=false; show you the login link; wait until BackendState=Running"
    Would "record the VPS's 100.x address and MagicDNS name in $TailnetFile"
}

# ---- [c] deploy the server change -------------------------------------------------------------
Step "c" "deploy robinhood_read_api.py (read key + a previous key that dies at a deadline)"
$deployed = $false
$deployedProm = $false
if ($Apply -and $rotate) {
    Assert-SafeToRestart "the Robinhood and Prometheus APIs"
    $bases = @(); if (-not $AllowServerDrift) { $bases = @($ServerBaseSha256.ToLower()) }
    $dep = Invoke-Remote @{ step = "deploy"; unit = $rhUnit; name = "robinhood_read_api.py"; stamp = $stamp; sha256 = $ServerSha256.ToLower()
                            content_b64 = [Convert]::ToBase64String($serverBytes); must_contain = @("PRO_RH_READ_KEY", "PRO_RH_API_KEY_PREVIOUS_UNTIL")
                            base_sha256 = $bases; path = $rhScript }
    if ($dep.unchanged) { Say "$($dep.path) is already this version" } else { Say "$($dep.path) replaced; backup $($dep.backup)"; $deployed = $true }
    $promScript = $null
    if ($disc -and $disc.apis.prometheus.script) { $promScript = [string]$disc.apis.prometheus.script }
    if ($promPinOk -and $promScript -and -not $promPrev) {
        $pb = @(); if (-not $AllowServerDrift) { $pb = @($PromBaseSha256.ToLower()) }
        $pd = Try-Remote @{ step = "deploy"; name = "webapp.py"; path = $promScript; stamp = $stamp; sha256 = $PromSha256.ToLower()
                            content_b64 = [Convert]::ToBase64String($promBytes); must_contain = @("PROM_READ_KEY", "PROM_API_KEY_PREVIOUS_UNTIL"); base_sha256 = $pb }
        if ($pd -and ($pd.deployed -or $pd.unchanged)) {
            if ($pd.deployed) { Say "$($pd.path) replaced (the auth change only); backup $($pd.backup)"; $deployedProm = $true }
            $promPrev = $true; $promRead = $true
        } else { Warn "Prometheus's webapp.py on the VPS is not main's version (or could not be replaced): its auth change is NOT deployed and its key is NOT rotated. Compare it with pantheon main (the commits it may be missing are listed below), deploy Prometheus with its own script, then run this again (or -AllowServerDrift)."; Show-PromUndeployed "the VPS's Prometheus may be missing" }
    }
    Mark "deploy"
} elseif ($Apply) { Say "no rotation: not deployed" }
else {
    Would "wait for a quiet moment first: outside 09:30-16:00 New York Mon-Fri, no prometheus-{scan,entry,review,execute} / mrcrab job running or due in 15 min, no Prometheus job in flight (-Force overrides)"
    Would "put trading-bot-app $($ServerCommit.Substring(0, 7)):server/robinhood_read_api.py (sha256-checked) over $rhScript - only if the running file is main's (the reviewed base); backup, compile check, same owner/mode"
    Would "put pantheon $($PromCommit.Substring(0, 7)):webapp.py (sha256-checked) over the VPS's prometheus/webapp.py - only if that is main's version (never the tree deploy)"
}

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
$rotated = @{ rh = $true; prom = ($promPrev -and [bool]$promEnv); kark = ($karkPrev -and [bool]$karkEnv) }
$files = @()
if ($rotate) {
    $files += @{ path = $rhEnv; set = @{ PRO_RH_API_KEY = $state.keys.rh_full; PRO_RH_READ_KEY = $state.keys.rh_read }
                 keep_previous = @{ PRO_RH_API_KEY = "PRO_RH_API_KEY_PREVIOUS" }; previous_days = $PreviousDays }
    $pf = @{ path = $promEnv; set = @{}; keep_previous = @{}; previous_days = $PreviousDays }
    if ($rotated.prom) { $pf.set.PROM_API_KEY = $state.keys.prom; $pf.keep_previous.PROM_API_KEY = "PROM_API_KEY_PREVIOUS" }
    elseif ($promEnv -and $disc) { Warn "Prometheus: its server has no grace window yet (pantheon branch read-key, deployed with its Deploy script) - PROM_API_KEY is NOT rotated, so the phone keeps working" }
    if ($promRead -and $promEnv) { $pf.set.PROM_READ_KEY = $state.keys.prom_read }
    if ($pf.set.Count) { $files += $pf }
    if ($rotated.kark) { $files += @{ path = $karkEnv; set = @{ KARKINOS_API_KEY = $state.keys.kark }; keep_previous = @{ KARKINOS_API_KEY = "KARKINOS_API_KEY_PREVIOUS" }; previous_days = $PreviousDays } }
    elseif ($karkEnv -and $disc) { Warn "Karkinos: its server has no grace window yet (Mr-Crab branch grace) - KARKINOS_API_KEY is NOT rotated, so the phone keeps working" }
}
$phoneKeyOk = $null
if ($Apply -and $rotate) {
    $envr = Invoke-Remote @{ step = "env"; stamp = $stamp; files = $files }
    foreach ($f in $envr.files) {
        if ($f.error) { Bad ("{0}: {1}" -f $f.path, $f.error); continue }
        Say ("{0}: changed [{1}]; backup {2}" -f $f.path, ((@($f.changed)) -join ", "), $f.backup)
        foreach ($pr in @($f.until.PSObject.Properties)) { if ($pr) { Say ("  {0}: the previous key dies at {1:u} (the VPS's clock)" -f $pr.Name, [DateTimeOffset]::FromUnixTimeSeconds([int64]$pr.Value).UtcDateTime) } }
        if ($f.path -eq $rhEnv) {
            $kept = $f.previous_fp.PRO_RH_API_KEY_PREVIOUS
            if (-not $kept) { Bad "PRO_RH_API_KEY_PREVIOUS is not set: the phone's current key would be dead" }
            elseif ($oldKeys.rh -and $oldKeys.rh -ne $state.keys.rh_full) {
                $phoneKeyOk = ($kept -eq (Get-Fingerprint $oldKeys.rh))
                if ($phoneKeyOk) { Say "the key kept for the phone is the one this machine had (fingerprints match)" }
                else { Warn "the key kept for the phone (the VPS's live key) is NOT the one this machine had: the phone's key is checked by the VPS's value only" }
            }
        }
    }
    if ($script:failures) { Stop-Here "an env file was not updated as intended: nothing was restarted. Fix it, then run -Apply again (the same keys are used)." }
    Save-State ((Load-State) | Add-Member -NotePropertyName rotated -NotePropertyValue ([pscustomobject]$rotated) -Force -PassThru)
    Mark "env"
} elseif (-not $Apply) {
    Would "back up $rhEnv, $promEnv, $karkEnv to <file>.bak-pionir-<stamp> (on the VPS, created 0600)"
    Would "$rhEnv : PRO_RH_API_KEY = new; PRO_RH_API_KEY_PREVIOUS = the LIVE key (the phone's) until -FinishRotation or the deadline; PRO_RH_READ_KEY = new"
    Would "$promEnv / $karkEnv : rotated the same way ONLY where the server has a grace window (else not rotated); PROM_READ_KEY where supported"
}

# ---- [e] restart and verify -------------------------------------------------------------------
Step "e" "restart where the running keys are stale (try-restart only) and verify through a temporary tunnel"
$failBefore = $script:failures
$eVerified = $false
if ($Apply -and $rotate) {
    $rhKeys = @("PRO_RH_API_KEY", "PRO_RH_READ_KEY", "PRO_RH_API_KEY_PREVIOUS", "PRO_RH_API_KEY_PREVIOUS_UNTIL")
    $units = @{}
    $units[$rhUnit] = @{ keys = $rhKeys; force = ($deployed -or -not (Done "deployed_restart")) }
    $unitWasActive = @{}
    $unitFiles = @{}
    $unitFiles[$rhUnit] = @($rhEnv, $rhScript)
    if ($promUnit) {
        $pk = @("PROM_API_KEY")
        if ($rotated.prom) { $pk += @("PROM_API_KEY_PREVIOUS", "PROM_API_KEY_PREVIOUS_UNTIL") }
        if ($promRead) { $pk += "PROM_READ_KEY" }
        $units[$promUnit] = @{ keys = $pk; force = $deployedProm }
        $unitFiles[$promUnit] = @($promEnv)
        if ($deployedProm -and $promScript) { $unitFiles[$promUnit] = @($promEnv, $promScript) }
    }
    if ($karkUnit) {
        $kk = @("KARKINOS_API_KEY")
        if ($rotated.kark) { $kk += @("KARKINOS_API_KEY_PREVIOUS", "KARKINOS_API_KEY_PREVIOUS_UNTIL") }
        $units[$karkUnit] = @{ keys = $kk }
        $unitFiles[$karkUnit] = @($karkEnv)
    }
    $rs = Invoke-Remote @{ step = "restart"; units = $units }
    $running = @{}
    foreach ($u in $rs.units) { $unitWasActive[$u.unit] = [bool]$u.was_active }   # all of them, before any rollback needs them
    foreach ($u in $rs.units) {
        $running[$u.unit] = ($u.state -eq "active")
        if ($u.armed_mismatch) { Stop-Here ("{0}: real-money orders differ between the running process and its configuration: NOT restarted. Decide first (Pionir proteus.rh_orders_off / on), then run again." -f $u.unit) }
        if (-not $u.was_active) { Warn ("{0}: {1} - {2}" -f $u.unit, $u.state, $u.note); continue }
        if ($u.current) { Say ("{0}: already running with its current keys (no restart)" -f $u.unit) }
        elseif (-not $u.restarted) {
            Bad ("{0} did not come back after its restart (state {1})" -f $u.unit, $u.state)
            Undo-Rotation $u.unit ("{0}: " -f $u.unit)
        } else { Say ("{0}: restarted; orders armed {1} -> {2}" -f $u.unit, $u.orders_armed_before, $u.orders_armed_after) }
        if (@($u.env_missing).Count) { Bad ("{0} runs without {1}" -f $u.unit, ((@($u.env_missing)) -join ", ")) }
        if (@($u.env_stale).Count) { Bad ("{0} runs with an old value of {1}" -f $u.unit, ((@($u.env_stale)) -join ", ")) }
        if ($u.orders_armed_before -ne $u.orders_armed_after) { Bad ("{0}: the orders switch changed across the restart ({1} -> {2})" -f $u.unit, $u.orders_armed_before, $u.orders_armed_after) }
    }
    $rhRunning = [bool]$running[$rhUnit]
    $promRunning = $promUnit -and [bool]$running[$promUnit]
    $karkRunning = $karkUnit -and [bool]$running[$karkUnit]
    if (Test-RemoteForwardRefused) { Say "the tunnel key cannot open a listener on the VPS (-R refused)" }
    else { Bad "the tunnel key COULD open a listener on the VPS (-R): sshd's Match block is not in force" }
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
                if ($oldKeys.rh -and $oldKeys.rh -ne $state.keys.rh_full) { Check "robinhood GET /status with the phone's current key" "GET" $b "/status" $oldKeys.rh "" 200 }
            } else { Warn "the Robinhood API is stopped (left stopped): not checked" }
            if ($promRunning) {
                Check "prometheus GET /api/health" "GET" ($b + 1) "/api/health" "" "" 200
                $pkey = $oldKeys.prom
                if ($rotated.prom) { $pkey = $state.keys.prom }
                if ($pkey) { Check "prometheus GET /status with the full key" "GET" ($b + 1) "/status" $pkey "" 200 }
                if ($rotated.prom -and $oldKeys.prom) { Check "prometheus GET /status with the phone's current key" "GET" ($b + 1) "/status" $oldKeys.prom "" 200 }
                if ($promRead) {
                    Check "prometheus GET /status with the READ key" "GET" ($b + 1) "/status" $state.keys.prom_read "" 200
                    Check "prometheus POST /pause with the READ key (must be refused)" "POST" ($b + 1) "/pause" $state.keys.prom_read "{}" 401
                }
            }
            if ($karkRunning) {
                Check "karkinos GET /health" "GET" ($b + 2) "/health" "" "" 200
                $kkey = $oldKeys.kark
                if ($rotated.kark) { $kkey = $state.keys.kark }
                if ($kkey) { Check "karkinos GET /status with its key" "GET" ($b + 2) "/status" $kkey "" 200 }
                if ($rotated.kark -and $oldKeys.kark) { Check "karkinos GET /status with the phone's current key" "GET" ($b + 2) "/status" $oldKeys.kark "" 200 }
            }
        } finally { Close-VerifyTunnel $vt }
    }
    $eVerified = ($script:failures -eq $failBefore)
    if ($eVerified) { Mark "deployed_restart"; Say "verified" }
    else {
        # ANY verification failure: one rule for all three APIs (Undo-Rotation): back to the
        # pre-rotation env when nothing has left the VPS, forward (keys kept) when it has
        Undo-Rotation "" ""
    }
} elseif (-not $Apply) {
    Would "restart a unit only when its RUNNING keys differ from its env files (or the server file is new): try-restart, a stopped unit stays stopped"
    Would "never restart one whose running PRO_RH_ORDERS_ENABLED differs from its configuration (that would arm/disarm): stop and ask"
    Would "a Robinhood API that does not come back: its env and server file restored from this run's backups, started again, stop"
    Would "check the tunnel key cannot open a listener (-R), then through a temporary tunnel (status codes only): READ key 200, READ key POST 401, full key 200, the phone's current key 200"
}

# ---- [f] this machine's keys ----------------------------------------------------------------
Step "f" "this machine's keys (the desktop gets READ keys only)"
$deskFiles = @("proteus-api-key.txt", "proteus-read-key.txt", "prometheus-api-key.txt", "prometheus-read-key.txt", "karkinos-read-key.txt")
$shipOk = (-not $rotate) -or $eVerified
if ($Apply -and $rotate -and -not $shipOk) {
    Bad "[e] was not fully verified: no new key is written here or sent to the phone. Fix the red lines, run -Apply again."
} elseif ($Apply -and $rotate) {
    Protect-Dir $vaultDir
    if (-not (Test-Path -LiteralPath $backupDir)) {
        Protect-Dir $backupDir
        foreach ($n in $deskFiles) { $src = Join-Path $secretsDir $n; if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $backupDir $n) } }
        Say "old key files backed up to $backupDir"
    }
    Write-Secret (Join-Path $secretsDir "proteus-read-key.txt") $state.keys.rh_read
    if ($promRead) { Write-Secret (Join-Path $secretsDir "prometheus-read-key.txt") $state.keys.prom_read }
    else { Warn "the desktop's Prometheus panel stays 'not configured' until the Prometheus server has PROM_READ_KEY (pantheon branch read-key)" }
    if ($rotated.kark) { Write-Secret (Join-Path $secretsDir "karkinos-read-key.txt") $state.keys.kark }
    # the full-power keys leave the folder the desktop reads: the vault holds them for the phone
    Write-Secret (Join-Path $vaultDir "proteus-api-key.txt") $state.keys.rh_full
    $promFull = Join-Path $secretsDir "prometheus-api-key.txt"
    if ($rotated.prom) { Write-Secret (Join-Path $vaultDir "prometheus-api-key.txt") $state.keys.prom }
    elseif (Test-Path -LiteralPath $promFull) { Copy-Item -LiteralPath $promFull -Destination (Join-Path $vaultDir "prometheus-api-key.txt") -Force }
    foreach ($full in @((Join-Path $secretsDir "proteus-api-key.txt"), $promFull)) {
        if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Force; Say "removed $full (a full-power key no longer sits where the desktop reads)" }
    }
    Say "desktop (secrets\): proteus-read-key.txt, prometheus-read-key.txt (if its server has one), karkinos-read-key.txt (its API is read-only)"
    Say "full-power keys: $vaultDir\{proteus,prometheus}-api-key.txt - read only by this script (to set the phone's secrets)"
    Mark "local"
} elseif (-not $Apply) {
    Would "back up $secretsDir\{proteus-api-key,prometheus-api-key,karkinos-read-key}.txt to $vaultDir\backup-<stamp>\"
    Would "write proteus-read-key.txt, prometheus-read-key.txt (READ keys), karkinos-read-key.txt; the full Robinhood and Prometheus keys move to $vaultDir"
}

# ---- [p] the phone ----------------------------------------------------------------------------
Step "p" "the phone: its keys, the tailnet address, its build"
$needSecrets = $rotate -and -not (Done "phone")
if ($Apply -and $rotate -and -not $shipOk) { $needSecrets = $false }
$needHost = $tailnet -and $tailnet.ipv4 -and ($tailnet.phone_build_host -ne $tailnet.ipv4)
if (-not $Apply) {
    Would "gh secret set -f <vault dotenv, deleted after> --repo $PhoneRepo (PRO_RH_API_KEY, PROM_API_KEY, KARKINOS_API_KEY)"
    Would "gh variable set VPS_HOST = the VPS's 100.x address; gh workflow run build.yml --ref main (main distributes it to your phone)"
} elseif ($rotate -and -not $shipOk) { Say "not built: [e] was not verified (see above)" }
elseif (-not $tailnet -or -not $tailnet.ipv4) { Bad "the VPS is not on the tailnet: the phone is not built (it would point at nothing)" }
elseif ($SkipPhone) { Warn ("-SkipPhone: set the GitHub secrets PRO_RH_API_KEY, PROM_API_KEY, KARKINOS_API_KEY and the variable VPS_HOST={0} of {1} yourself, then run its 'Build & Distribute APK' workflow on main" -f $tailnet.ipv4, $PhoneRepo) }
elseif (-not $needSecrets -and -not $needHost) { Say "the phone's build for this rotation and this tailnet address was already started" }
else {
    $gh = Find-Exe "gh"
    $authOk = $false
    if ($gh) { $authOk = ((Invoke-Native $gh @("auth", "status") "" 60).Code -eq 0) }
    if (-not $authOk) { Bad "gh (GitHub CLI) is missing or not logged in: set the phone's secrets and VPS_HOST by hand (values: $vaultDir\proteus-api-key.txt, $secretsDir\prometheus-api-key.txt, $secretsDir\karkinos-read-key.txt), then run the build" }
    else {
        $okAll = $true
        if ($needSecrets) {
            # the values go in a dotenv file in the owner-only vault, read by gh and deleted at
            # once: never on a command line, and never through a console encoding
            $names = @("PRO_RH_API_KEY")
            $lines = @("PRO_RH_API_KEY=" + $state.keys.rh_full)
            if ($rotated.prom) { $names += "PROM_API_KEY"; $lines += ("PROM_API_KEY=" + $state.keys.prom) }
            if ($rotated.kark) { $names += "KARKINOS_API_KEY"; $lines += ("KARKINOS_API_KEY=" + $state.keys.kark) }
            $dotenv = Join-Path $vaultDir "phone-secrets.env"
            Write-Secret $dotenv (($lines -join "`n") + "`n")
            try { $r = Invoke-Native $gh @("secret", "set", "-f", $dotenv, "--repo", $PhoneRepo) "" 180 }
            finally { Remove-Item -LiteralPath $dotenv -Force -ErrorAction SilentlyContinue }
            $okAll = ($r.Code -eq 0)
            if ($okAll) { Say ("GitHub secrets {0} of {1} set" -f ($names -join ", "), $PhoneRepo) } else { Bad ("gh secret set: " + (Tail $r.Err)) }
        }
        if ($okAll) {
            # the address is not a secret: a repo variable, passed as an argument
            $vr = Invoke-Native $gh @("variable", "set", "VPS_HOST", "--body", [string]$tailnet.ipv4, "--repo", $PhoneRepo) "" 120
            if ($vr.Code -eq 0) { Say ("GitHub variable VPS_HOST = {0} (the app reads the VPS over the tailnet)" -f $tailnet.ipv4) } else { $okAll = $false; Bad ("gh variable set VPS_HOST: " + (Tail $vr.Err)) }
        }
        if ($okAll) {
            $wr = Invoke-Native $gh @("workflow", "run", "build.yml", "--repo", $PhoneRepo, "--ref", "main") "" 120
            if ($wr.Code -eq 0) {
                Say "the phone's build started (main distributes it to your phone through Firebase App Distribution)"
                if ($needSecrets) { Mark "phone" }
                $tn = Read-Tailnet
                $tn | Add-Member -NotePropertyName phone_build_host -NotePropertyValue ([string]$tailnet.ipv4) -Force
                Save-Tailnet $tn
            } else { Bad ("gh workflow run build.yml: " + (Tail $wr.Err)) }
        }
    }
}

# ---- [g] the public ports ---------------------------------------------------------------------
Step "g" "the public ports"
Say "moved to the SSH tunnel by this change: Pionir Desktop (all three). On the VPS itself (loopback, unaffected): daily_digest.py, deploy_running_service.sh, Pionir's proteus.status."
foreach ($c in $PublicConsumers) { Say (":{0} stays open for now - used over the internet by {1} (the OLD build)" -f $c.Port, $c.Who) }
Say "they close in -FinishRotation -Apply, once you confirm the NEW build reads over the tailnet (tailscale0 stays open, with an automatic revert)."

# ---- [h] rollback -----------------------------------------------------------------------------
Step "h" "rollback (only if something is wrong)"
if (-not $Apply) { $stamp = "<stamp>" }
$bk = ".bak-pionir-$stamp"
$envList = @($rhEnv, $promEnv, $karkEnv) | Where-Object { $_ }
$unitList = (@($rhUnit, $promUnit, $karkUnit) | Where-Object { $_ }) -join " "
Say "1. the VPS, as root (ssh -i $DeployKey root@$VpsHost):"
foreach ($e in $envList) { Say ("     cp -p {0}{1} {0}" -f $e, $bk) }
Say ("     cp -p {0}{1} {0}      (the server file)" -f $rhScript, $bk)
Say "     cp -p <prometheus/webapp.py>$bk <prometheus/webapp.py>   (if [c] replaced it: the path it printed)"
Say ("     systemctl try-restart {0}" -f $unitList)
Say "2. this machine's key files:  Copy-Item $vaultDir\backup-$stamp\* $secretsDir\"
Say ("3. the phone: Get-Content -Raw $vaultDir\backup-$stamp\proteus-api-key.txt | gh secret set PRO_RH_API_KEY --repo {0}  (and PROM_API_KEY, KARKINOS_API_KEY from the same folder), then gh workflow run build.yml --repo {0} --ref main" -f $PhoneRepo)
Say "4. the tunnel account: ssh ... root@$VpsHost `"rm -f /home/$TunnelUser/.ssh/authorized_keys`"   (or: userdel -r $TunnelUser)"
Say "   and its sshd Match block: rm /etc/ssh/sshd_config.d/60-pionir-tunnel.conf (or the pionir-tunnel block at the end of sshd_config), sshd -t, systemctl reload ssh"
Say "5. the firewall (only after -FinishRotation closed the ports): ssh ... root@$VpsHost `"sh /root/.pionir-ufw-<finish stamp>/revert.sh`""
Say "   nginx sites it moved aside: mv /root/.pionir-nginx-<finish stamp>/* /etc/nginx/sites-enabled/ ; then nginx -t, and if it passes: systemctl reload nginx"
Say "   (the old keys also die by themselves at the deadline the servers enforce: at most $PreviousDays days after -Apply)"
Say "6. the tailnet: ssh ... root@$VpsHost `"tailscale down`" (remove the node in the Tailscale admin console; apt-get remove tailscale to uninstall); gh variable delete VPS_HOST --repo $PhoneRepo and rebuild the phone"
Say "7. then delete $stateFile so the next run starts clean."

Write-Host ""
if (-not $Apply) { Write-Host "  dry run: nothing was changed. Read it, then run with -Inspect, then -Apply." -ForegroundColor DarkCyan; exit 0 }
if ($script:failures) { Write-Host ("  {0} step(s) FAILED - read the red lines above. The keys are kept; running -Apply again repeats only what is needed." -f $script:failures) -ForegroundColor Red; exit 1 }
if ($rotate) {
    Write-Host "  done. The desktop reads through the tunnel pane (pionir.ps1, or Pionir Desktop's 'VPS tunnel')." -ForegroundColor Green
    Write-Host "  When the phone has installed its new build: .\tools\vps-lockdown.ps1 -FinishRotation -Apply" -ForegroundColor Green
    Write-Host "  (it asks you to confirm the new app reads over the tailnet, then ends the old key and closes 8000-8002 to the internet)" -ForegroundColor Green
} else { Write-Host "  done." -ForegroundColor Green }
exit 0
