[CmdletBinding()]
param(
    [string] $Repo = (Split-Path -Parent $PSScriptRoot),
    [string] $AtaniExe = 'C:\src\Atani\.venv\Scripts\atani.exe',
    [string] $BryoPython = 'C:\src\terrarium\.venv\Scripts\python.exe',
    [string] $AutogenesisPython = 'C:\src\autogenesis\.venv\Scripts\python.exe',
    [string] $SpecialistsFile = (Join-Path $Repo 'specialists.toml')
)

$ErrorActionPreference = 'Stop'
$Pythonw = Join-Path $Repo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $Pythonw)) {
    throw "Pionir's environment is not installed at $Pythonw. Run scripts\bootstrap.ps1 first."
}

$env:PIONIR_STATE_ROOT = Join-Path $env:USERPROFILE '.pionir'
if (Test-Path $AtaniExe) {
    $env:PIONIR_ATANI_COMMAND_JSON = ConvertTo-Json -Compress -InputObject @($AtaniExe)
}
if (Test-Path $BryoPython) {
    $env:PIONIR_BRYO_STATUS_COMMAND_JSON = ConvertTo-Json -Compress -InputObject @(
        $BryoPython,
        '-m',
        'bryo.status'
    )
}
$AutogenesisState = 'C:\src\autogenesis\state-live\autogenesis.sqlite3'
if ((Test-Path $AutogenesisPython) -and (Test-Path $AutogenesisState)) {
    $env:PIONIR_AUTOGENESIS_STATUS_COMMAND_JSON = ConvertTo-Json -Compress -InputObject @(
        $AutogenesisPython,
        '-m',
        'autogenesis',
        '--state-dir',
        'C:\src\autogenesis\state-live',
        'status'
    )
}
if (Test-Path $SpecialistsFile) {
    $env:PIONIR_SPECIALISTS_FILE = $SpecialistsFile
}

$TokenFile = Join-Path $env:USERPROFILE '.techsupport_agent\bridge_token.txt'
if (-not $env:PIONIR_THEO_TOKEN -and (Test-Path $TokenFile)) {
    $env:PIONIR_THEO_TOKEN = (Get-Content $TokenFile -Raw).Trim()
}

& $Pythonw -m pionir.desktop
