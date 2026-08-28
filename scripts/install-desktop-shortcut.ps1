[CmdletBinding()]
param(
    [string] $Repo = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$LaunchScript = Join-Path $Repo 'scripts\launch-desktop.ps1'
if (-not (Test-Path $LaunchScript)) {
    throw "Pionir's desktop launcher is missing at $LaunchScript."
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'Pionir.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = 'powershell.exe'
$Shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$LaunchScript`""
$Shortcut.WorkingDirectory = $Repo
$Shortcut.Description = 'Pionir specialist-agent desktop console'
$Shortcut.Save()

Write-Host "Created $ShortcutPath"
