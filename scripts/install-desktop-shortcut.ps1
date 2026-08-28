[CmdletBinding()]
param(
    [string] $Repo = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$Pythonw = Join-Path $Repo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $Pythonw)) {
    throw "Pionir's environment is not installed at $Pythonw. Run scripts\bootstrap.ps1 first."
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'Pionir.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = '-m pionir.desktop'
$Shortcut.WorkingDirectory = $Repo
$Shortcut.Description = 'Pionir specialist-agent desktop console'
$Shortcut.Save()

Write-Host "Created $ShortcutPath"
