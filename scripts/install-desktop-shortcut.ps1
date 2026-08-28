[CmdletBinding()]
param(
    [string] $Repo = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$Launcher = Join-Path $Repo '.venv\Scripts\pionir-desktop.exe'
if (-not (Test-Path $Launcher)) {
    throw "Pionir Desktop is not installed at $Launcher. Run scripts\bootstrap.ps1 first."
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'Pionir.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Launcher
$Shortcut.WorkingDirectory = $Repo
$Shortcut.Description = 'Pionir specialist-agent desktop console'
$Shortcut.Save()

Write-Host "Created $ShortcutPath"
