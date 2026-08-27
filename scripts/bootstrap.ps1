[CmdletBinding()]
param(
    [string]$PythonLauncher = "py",
    [string[]]$PythonArguments = @("-3.12"),
    [string]$Venv = ".venv"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repo $Venv

Push-Location $repo
try {
    & $PythonLauncher @PythonArguments -m venv $venvPath
    $pythonExe = Join-Path $venvPath "Scripts\python.exe"
    & $pythonExe -m pip install --upgrade pip
    & $pythonExe -m pip install -e .
    & $pythonExe -m unittest discover -s tests -v
    Write-Host "Pionir is installed. Configure .env values in your user environment, then run:"
    Write-Host "  $venvPath\Scripts\pionir.exe doctor"
}
finally {
    Pop-Location
}
