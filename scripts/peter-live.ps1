<#
.SYNOPSIS
    Peter (C:\src\The-Web) as a Pionir companion: exactly what deploy\Peter.cmd runs.

.DESCRIPTION
    Run from Peter's repo root (pionir.ps1's pane and Pionir Desktop both start it there).
    It does what Peter.cmd does, and nothing else: dot-source Peter's Alpaca PAPER
    credentials from deploy\peter-secrets.ps1 if the file exists (the values go into this
    process's environment and are never printed), then run `deploy\peter.ps1 live` - the
    one process that serves http://127.0.0.1:8790 and runs the collect/journal/P&L loop.

    Why a script and not the one-line -Command Peter.cmd passes: the launcher's panes and
    the desktop's process plan must name the same command, and a -File path is one both
    can spell without nested quoting. The-Web is not edited.

    Peter's model calls reach the card through Pionir's Ollama gate because the caller sets
    HTTP_PROXY / NO_PROXY for this process (pionir.ps1's $peterPrelude) - not here.

    Exit code: Peter's own (peter.ps1 passes it through).
#>
$ErrorActionPreference = 'Stop'
$here = (Get-Location).Path
if (-not (Test-Path (Join-Path $here 'deploy\peter.ps1'))) {
    Write-Host "  peter-live.ps1 must run from Peter's repo root; $here has no deploy\peter.ps1." -ForegroundColor Red
    exit 2
}
$secrets = Join-Path $here 'deploy\peter-secrets.ps1'
if (Test-Path $secrets) { . $secrets }
& (Join-Path $here 'deploy\peter.ps1') live
exit $LASTEXITCODE
